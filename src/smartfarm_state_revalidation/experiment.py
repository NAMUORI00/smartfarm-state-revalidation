from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from random import Random
from typing import Any

from .contracts import (
    ActionCandidate,
    ActionPrecondition,
    AnswerUnitSpec,
    Barrier,
    GenerationRequest,
    PredicateOperator,
    RelatedStateSpec,
    ScheduledPatch,
    StateDependency,
    StatePatch,
    StateSnapshot,
)
from .dependency import affected_unit_ids
from .engine import BarrierPatchInjector, ExperimentEngine, InMemoryStateStore
from .evaluation.runner import record_from_run_result
from .fingerprint import compare_related
from .executor import DryRunExecutor
from .generator import SnapshotEchoGenerator
from .policies import policy_for
from .trace import TraceRecorder


FIXED_TIME = datetime(2026, 8, 7, tzinfo=timezone.utc)


def load_json(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def evaluation_protocol(
    protocol: Mapping[str, Any],
    related_state: Mapping[str, Any],
) -> dict[str, Any]:
    merged = dict(protocol)
    merged["state_keys"] = list(related_state.get("state_keys", ()))
    merged["dependency_profiles"] = list(related_state.get("dependency_profiles", ()))
    return merged


def _numeric_boundaries(rule: Mapping[str, Any]) -> tuple[float, ...]:
    boundaries: set[float] = set()
    for band in rule.get("semantic_bands", ()):
        if not isinstance(band, Mapping):
            continue
        for name in ("lower_inclusive", "lower_exclusive", "upper_inclusive", "upper_exclusive"):
            value = band.get(name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                boundaries.add(float(value))
    return tuple(sorted(boundaries))


def related_state_spec(
    profile_id: str,
    related_state: Mapping[str, Any],
) -> RelatedStateSpec:
    rules = {
        str(item["key"]): item
        for item in related_state.get("state_keys", ())
        if isinstance(item, Mapping) and isinstance(item.get("key"), str)
    }
    profile = next(
        (
            item
            for item in related_state.get("dependency_profiles", ())
            if isinstance(item, Mapping) and item.get("profile_id") == profile_id
        ),
        None,
    )
    if profile is None:
        raise KeyError(f"unknown dependency profile: {profile_id}")
    dependencies: list[StateDependency] = []
    for key in profile.get("required_state_keys", ()):
        key = str(key)
        rule = rules.get(key, {})
        tolerance = rule.get("minimum_meaningful_delta", rule.get("absolute_epsilon", 0.0))
        dependencies.append(
            StateDependency(
                key=key,
                tolerance=float(tolerance),
                boundaries=_numeric_boundaries(rule),
                unit=str(rule["unit"]) if isinstance(rule.get("unit"), str) else None,
            )
        )
    return RelatedStateSpec(
        profile_id=profile_id,
        dependencies=tuple(dependencies),
        unit_specs=_answer_unit_specs(profile),
    )


def _answer_unit_specs(profile: Mapping[str, Any]) -> tuple[AnswerUnitSpec, ...]:
    """프로파일이 선언한 답변 단위를 읽는다.

    선언이 없으면 빈 튜플을 돌려주고 상태키 하나가 곧 단위 하나인 기본 구성을 쓴다.
    v1 프로파일은 단위를 선언하지 않으므로 기존 동작이 그대로 유지된다.
    """
    declared = profile.get("answer_units", ())
    if not isinstance(declared, Sequence) or isinstance(declared, (str, bytes)):
        return ()
    specs: list[AnswerUnitSpec] = []
    for item in declared:
        if not isinstance(item, Mapping):
            raise ValueError("every answer unit declaration must be a JSON object")
        specs.append(
            AnswerUnitSpec(
                unit_id=str(item["unit_id"]),
                depends_on=tuple(str(key) for key in item.get("depends_on", ())),
            )
        )
    return tuple(specs)


def _value(snapshot: StateSnapshot, key: str) -> Any:
    return snapshot.values.get(key)


def _root_zone_candidate(
    request: GenerationRequest,
    candidate: Mapping[str, Any],
    zone_id: str,
) -> ActionCandidate | None:
    temperature_key = f"sensor.{zone_id}.root_zone_temperature_c"
    alert_key = f"alert.{zone_id}.root_temperature_high.active"
    occurrence_key = f"alert.{zone_id}.root_temperature_high.occurrence_id"
    device_key = f"device.{zone_id}.root_zone_chiller.enabled"
    available_key = "telemetry.farm.available"
    age_key = "telemetry.farm.sample_age_seconds"
    temperature = _value(request.snapshot, temperature_key)
    age = _value(request.snapshot, age_key)
    eligible = (
        isinstance(temperature, (int, float))
        and not isinstance(temperature, bool)
        and float(temperature) > 18.0
        and _value(request.snapshot, alert_key) is True
        and _value(request.snapshot, device_key) is False
        and _value(request.snapshot, available_key) is True
        and isinstance(age, (int, float))
        and not isinstance(age, bool)
        and float(age) <= 120.0
    )
    if not eligible:
        return None
    preconditions = [
        ActionPrecondition(temperature_key, PredicateOperator.GT, 18.0),
        ActionPrecondition(alert_key, PredicateOperator.EQ, True),
        ActionPrecondition(device_key, PredicateOperator.EQ, False),
        ActionPrecondition(available_key, PredicateOperator.EQ, True),
        ActionPrecondition(age_key, PredicateOperator.LE, 120.0),
    ]
    occurrence = _value(request.snapshot, occurrence_key)
    if occurrence is not None:
        preconditions.append(ActionPrecondition(occurrence_key, PredicateOperator.EQ, occurrence))
    return ActionCandidate(
        action=str(candidate["action"]),
        target=str(candidate["target"]),
        value=candidate.get("value"),
        zone_id=zone_id,
        bound_state_revision=request.snapshot.revision,
        preconditions=tuple(preconditions),
    )


def _humidity_candidate(
    request: GenerationRequest,
    candidate: Mapping[str, Any],
    zone_id: str,
) -> ActionCandidate | None:
    humidity_key = f"sensor.{zone_id}.relative_humidity_pct"
    alert_key = f"alert.{zone_id}.humidity_high.active"
    occurrence_key = f"alert.{zone_id}.humidity_high.occurrence_id"
    health_key = f"alert.{zone_id}.air_temperature_sensor_health.active"
    device_key = f"device.{zone_id}.circulation_fan.percent"
    available_key = "telemetry.farm.available"
    age_key = "telemetry.farm.sample_age_seconds"
    humidity = _value(request.snapshot, humidity_key)
    fan = _value(request.snapshot, device_key)
    age = _value(request.snapshot, age_key)
    eligible = (
        isinstance(humidity, (int, float))
        and not isinstance(humidity, bool)
        and float(humidity) > 80.0
        and _value(request.snapshot, alert_key) is True
        and _value(request.snapshot, health_key) is False
        and isinstance(fan, (int, float))
        and not isinstance(fan, bool)
        and float(fan) < float(candidate.get("value", 80.0))
        and _value(request.snapshot, available_key) is True
        and isinstance(age, (int, float))
        and not isinstance(age, bool)
        and float(age) <= 120.0
    )
    if not eligible:
        return None
    preconditions = [
        ActionPrecondition(humidity_key, PredicateOperator.GT, 80.0),
        ActionPrecondition(alert_key, PredicateOperator.EQ, True),
        ActionPrecondition(health_key, PredicateOperator.EQ, False),
        ActionPrecondition(device_key, PredicateOperator.EQ, fan),
        ActionPrecondition(available_key, PredicateOperator.EQ, True),
        ActionPrecondition(age_key, PredicateOperator.LE, 120.0),
    ]
    occurrence = _value(request.snapshot, occurrence_key)
    if occurrence is not None:
        preconditions.append(ActionPrecondition(occurrence_key, PredicateOperator.EQ, occurrence))
    return ActionCandidate(
        action=str(candidate["action"]),
        target=str(candidate["target"]),
        value=candidate.get("value"),
        zone_id=zone_id,
        bound_state_revision=request.snapshot.revision,
        preconditions=tuple(preconditions),
    )


def action_factory(scenario: Mapping[str, Any]) -> Callable[[GenerationRequest], ActionCandidate | None]:
    contract = scenario.get("action_contract", {})
    candidate = contract.get("initial_candidate") if isinstance(contract, Mapping) else None
    zone_id = str(scenario.get("zone_id", "zone-a"))

    def create(request: GenerationRequest) -> ActionCandidate | None:
        if not isinstance(candidate, Mapping):
            return None
        action = candidate.get("action")
        if action == "set_cooling_enabled":
            return _root_zone_candidate(request, candidate, zone_id)
        if action == "set_circulation_fan_percent":
            return _humidity_candidate(request, candidate, zone_id)
        raise ValueError(f"unsupported research action: {action}")

    return create


def scheduled_patches(scenario: Mapping[str, Any]) -> tuple[ScheduledPatch, ...]:
    initial = scenario["initial_state"]
    previous_revision = int(initial["state_revision"])
    patches: list[ScheduledPatch] = []
    for order, item in enumerate(scenario.get("injections", ())):
        revision = int(item["state_revision"])
        if revision <= previous_revision:
            raise ValueError(f"scenario {scenario['scenario_id']} revisions must increase")
        patches.append(
            ScheduledPatch(
                barrier=Barrier(str(item["barrier"])),
                patch=StatePatch(
                    values=dict(item.get("patch", {})),
                    revision_delta=revision - previous_revision,
                    observed_at=FIXED_TIME + timedelta(seconds=revision),
                ),
                order=order,
            )
        )
        previous_revision = revision
    return tuple(patches)


def _evidence(scenario: Mapping[str, Any], suite: Mapping[str, Any]) -> tuple[str, ...]:
    evidence_id = scenario.get("fixed_evidence_set_id")
    for item in suite.get("fixed_evidence_sets", ()):
        if isinstance(item, Mapping) and item.get("evidence_set_id") == evidence_id:
            return tuple(str(value) for value in item.get("fixture_facts", ()))
    raise KeyError(f"unknown evidence set: {evidence_id}")


def _oracle_affected_units(
    spec: RelatedStateSpec,
    initial: Mapping[str, Any],
    release_truth: Mapping[str, Any],
) -> tuple[str, ...]:
    """선언된 정답 상태만으로 어떤 답변 단위가 수정되어야 하는지 판정한다."""
    facts = release_truth.get("facts")
    if not isinstance(facts, Mapping) or not facts:
        return ()
    before = StateSnapshot(
        revision=int(initial["state_revision"]),
        values=dict(initial["facts"]),
        observed_at=FIXED_TIME,
    )
    after = StateSnapshot(
        revision=int(initial["state_revision"]) + 1,
        values={**dict(initial["facts"]), **dict(facts)},
        observed_at=FIXED_TIME,
    )
    return affected_unit_ids(spec, compare_related(before, after, spec))


def run_scenario(
    scenario: Mapping[str, Any],
    suite: Mapping[str, Any],
    related_state: Mapping[str, Any],
    condition_id: str,
) -> dict[str, Any]:
    initial = scenario["initial_state"]
    snapshot = StateSnapshot(
        revision=int(initial["state_revision"]),
        values=dict(initial["facts"]),
        observed_at=FIXED_TIME + timedelta(seconds=int(initial["state_revision"])),
    )
    spec = related_state_spec(str(scenario["dependency_profile_id"]), related_state)
    store = InMemoryStateStore(snapshot)
    generator = SnapshotEchoGenerator(action_factory(scenario))
    trace = TraceRecorder()
    engine = ExperimentEngine(
        store,
        generator,
        DryRunExecutor(),
        trace_sink=trace,
        injector=BarrierPatchInjector(store, scheduled_patches(scenario)),
    )
    result = engine.run(
        policy_for(condition_id),
        question=str(scenario["question"]),
        related_state=spec,
        evidence=_evidence(scenario, suite),
    )
    action_contract = scenario.get("action_contract", {})
    record = record_from_run_result(
        result,
        episode_id=str(scenario["scenario_id"]),
        pair_id=str(scenario.get("pair_id", scenario["scenario_id"])),
        related_state_keys=spec.keys,
        invalid_action_opportunity=bool(action_contract.get("invalid_action_opportunity")),
        valid_action_opportunity=bool(action_contract.get("valid_action_opportunity")),
        calls={
            "policy_state_snapshot": (
                1
                if condition_id == "B0"
                else 3 + int(result.refresh_count)
            ),
            "oracle_state_snapshot": 3 + int(result.refresh_count),
        },
        latencies_ms={},
    )
    release_truth = scenario.get("release_truth", {})
    execution_truth = scenario.get("execution_truth", release_truth)
    # 오라클 기준 영향 단위. 선언된 초기 상태와 응답 시점 정답 상태만 사용하며
    # 실행 결과를 보지 않으므로 조건별로 같은 값이 나온다.
    record["answer_units"]["oracle_affected_unit_ids"] = list(
        _oracle_affected_units(spec, initial, release_truth)
    )
    record["comparison_scope"] = str(scenario.get("comparison_scope", "paired_primary"))
    record["oracle"] = {
        "initial_truth": dict(initial["facts"]),
        "release_truth": dict(release_truth.get("facts", {})),
        "execution_truth": dict(execution_truth.get("facts", {})),
        "initial_candidate": action_contract.get("initial_candidate"),
        "expected_action_candidate": action_contract.get("expected_release_candidate"),
        "execution_preconditions": list(action_contract.get("execution_preconditions", ())),
        "declared_invalid_action_opportunity": action_contract.get("invalid_action_opportunity"),
        "declared_valid_action_opportunity": action_contract.get("valid_action_opportunity"),
    }
    record.update(
        {
            "scenario_category": scenario.get("category"),
            "expected_change_class": scenario.get("expected_change_class"),
            "failure_code": result.failure_code.value if result.failure_code is not None else None,
            "source_revision": result.response.source_revision,
            "seed": scenario.get("seed"),
        }
    )
    return record


def run_suite(
    suite: Mapping[str, Any],
    related_state: Mapping[str, Any],
    *,
    conditions: Sequence[str] = ("B0", "B1", "B2"),
    diagnostic_conditions: Sequence[str] = ("B2",),
) -> list[dict[str, Any]]:
    """비교 조건별로 시나리오를 실행한다.

    diagnostic_conditions 는 `B2_safety_diagnostic` 범위 시나리오에 적용할 조건이다.
    갱신 예산을 가진 조건만 이 진단의 대상이 되므로 v2 에서는 SR 도 포함한다.
    기본값은 v1 동작을 그대로 유지한다.
    """
    diagnostic = set(diagnostic_conditions)
    records: list[dict[str, Any]] = []
    for scenario in suite.get("scenarios", ()):
        if not isinstance(scenario, Mapping):
            raise ValueError("every scenario must be a JSON object")
        applicable = list(conditions)
        if scenario.get("comparison_scope") == "B2_safety_diagnostic":
            applicable = [condition for condition in applicable if condition in diagnostic]
        Random(int(scenario.get("seed", 0))).shuffle(applicable)
        for order, condition in enumerate(applicable):
            record = run_scenario(scenario, suite, related_state, condition)
            record["condition_order_index"] = order
            records.append(record)
    return records
