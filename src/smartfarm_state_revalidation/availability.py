"""Field-level availability and joint dry-run action handling.

An opt-in research extension. Existing frozen engine/generator/executor remain
unchanged. Both policies share this joint orchestration; 'legacy' is a reference
policy, not a claim that the old single-action engine supported joint execution.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol

from .contracts import ActionPrecondition, GenerationRequest, PredicateOperator, StateSnapshot
from .executor import DryRunExecutor
from .experiment import action_factory, related_state_spec
from .fingerprint import compare_related

ROOT = "sensor.zone-a.root_zone_temperature_c"
HUMIDITY = "sensor.zone-a.relative_humidity_pct"
COOLER = "device.zone-a.root_zone_chiller.enabled"
FAN = "device.zone-a.circulation_fan.percent"
PERMIT_COOLER = "permission.zone-a.root_zone_chiller"
PERMIT_FAN = "permission.zone-a.circulation_fan"
AVAILABLE = "telemetry.farm.available"
AGE = "telemetry.farm.sample_age_seconds"
ROOT_ALERT = "alert.zone-a.root_temperature_high.active"
ROOT_EVENT = "alert.zone-a.root_temperature_high.occurrence_id"
HUMIDITY_ALERT = "alert.zone-a.humidity_high.active"
HUMIDITY_EVENT = "alert.zone-a.humidity_high.occurrence_id"
HEALTH = "alert.zone-a.air_temperature_sensor_health.active"
ALL_KEYS = (ROOT, HUMIDITY, COOLER, FAN, PERMIT_COOLER, PERMIT_FAN, AVAILABLE,
            ROOT_ALERT, ROOT_EVENT, HUMIDITY_ALERT, HUMIDITY_EVENT, HEALTH)
ACTION_KEYS = {
    "cooling": (ROOT, COOLER, PERMIT_COOLER, AVAILABLE, ROOT_ALERT, ROOT_EVENT),
    "fan": (HUMIDITY, FAN, PERMIT_FAN, AVAILABLE, HUMIDITY_ALERT, HUMIDITY_EVENT, HEALTH),
}


class FieldReadError(OSError):
    """Only one source field failed; other source fields remain readable."""


class SnapshotUnavailable(OSError):
    """The source cannot supply any field at this stage."""


class FieldSource(Protocol):
    def begin(self, stage: str, at: datetime) -> None: ...
    def read(self, key: str) -> tuple[object, datetime]: ...


@dataclass(frozen=True)
class Reading:
    key: str
    value: object
    observed_at: datetime | None
    status: str
    detail: str | None = None


@dataclass(frozen=True)
class Observation:
    stage: str
    at: datetime
    fields: tuple[Reading, ...]

    def get(self, key):
        return next(r for r in self.fields if r.key == key)


def collect(source: FieldSource, stage: str, at: datetime) -> Observation:
    """Translate declared I/O failures; programming errors are never suppressed."""
    try:
        source.begin(stage, at)
    except SnapshotUnavailable as error:
        return Observation(stage, at, tuple(Reading(k, None, None, "snapshot_unavailable", str(error)) for k in ALL_KEYS))
    readings = []
    for key in ALL_KEYS:
        try:
            value, observed_at = source.read(key)
            if value is None:
                readings.append(Reading(key, None, observed_at, "missing"))
            elif observed_at.tzinfo is None or observed_at > at:
                readings.append(Reading(key, None, observed_at, "invalid_timestamp"))
            else:
                age = (at - observed_at).total_seconds()
                readings.append(Reading(key, value, observed_at, "stale" if age > 120 else "current"))
        except KeyError:
            readings.append(Reading(key, None, None, "missing"))
        except FieldReadError as error:
            readings.append(Reading(key, None, None, "read_error", str(error)))
    return Observation(stage, at, tuple(readings))


def _render(reading: Reading, policy: str, previous: Reading | None = None):
    label = "근권 온도" if reading.key == ROOT else "습도"
    unit = "°C" if reading.key == ROOT else "%"
    if policy == "guarded" and reading.status != "current":
        historical = previous if previous and previous.status == "current" else None
        text = f"현재 {label} 확인 불가 ({reading.status})"
        if historical:
            text += f" · 이전 관측 {historical.value}{unit} ({historical.observed_at.isoformat()})"
        return {"key": reading.key, "status": "unavailable", "reason": reading.status,
                "value": None, "observed_at": None, "text": text,
                "historical": None if historical is None else {"value": historical.value, "observed_at": historical.observed_at.isoformat()}}
    return {"key": reading.key, "status": "current" if reading.value is not None else "unverified",
            "reason": None, "value": reading.value,
            "observed_at": reading.observed_at.isoformat() if reading.observed_at else None,
            "historical": None, "text": f"{label} {reading.value}{unit}"}


def _legacy_read_guard(observation: Observation):
    failed = next((r for r in observation.fields if r.status in {"read_error", "snapshot_unavailable"}), None)
    if failed:
        raise SnapshotUnavailable(f"{observation.stage}:{failed.key}:{failed.status}")


def _snapshot(observation: Observation, target: str, revision: int):
    values = {r.key: r.value for r in observation.fields if r.value is not None}
    measured = [observation.get(k) for k in ACTION_KEYS[target] if observation.get(k).observed_at]
    values[AGE] = max(((observation.at - r.observed_at).total_seconds() for r in measured), default=0)
    return StateSnapshot(revision, values, observation.at)


def _hold(target, reason):
    return {"target": target, "status": "hold", "reason": reason, "candidate": None,
            "applied": False, "executor_reached": False, "execution": None}


def _blocked(observation: Observation, target: str, policy: str):
    if policy == "guarded":
        invalid = next((observation.get(k) for k in ACTION_KEYS[target]
                        if observation.get(k).status != "current"), None)
        if invalid:
            return invalid.status
    permission = PERMIT_COOLER if target == "cooling" else PERMIT_FAN
    if observation.get(permission).value is not True:
        return "permission_denied"
    return None


def propose(observation, target, policy, registry):
    reason = _blocked(observation, target, policy)
    if reason:
        return _hold(target, reason)
    action = "set_cooling_enabled" if target == "cooling" else "set_circulation_fan_percent"
    device = "root_zone_chiller" if target == "cooling" else "circulation_fan"
    profile = "root-zone-cooling-control-v1" if target == "cooling" else "humidity-fan-control-v1"
    factory = action_factory({"zone_id": "zone-a", "action_contract": {"initial_candidate": {
        "action": action, "target": device, "value": True if target == "cooling" else 25.0}}})
    request = GenerationRequest("fixed fixture question", (), related_state_spec(profile, registry),
                                _snapshot(observation, target, 1), 0)
    candidate = factory(request)
    if candidate is None:
        return {**_hold(target, "no_action" if policy == "legacy" else "condition_not_met"), "status": "not_needed"}
    permission = PERMIT_COOLER if target == "cooling" else PERMIT_FAN
    candidate = replace(candidate, preconditions=candidate.preconditions + (
        ActionPrecondition(permission, PredicateOperator.EQ, True),))
    return {"target": target, "status": "ready", "reason": None, "candidate": candidate,
            "applied": False, "executor_reached": False, "execution": None}


def run_joint(source: FieldSource, policy: str, registry, times: dict[str, datetime]):
    if policy not in {"legacy", "guarded"}:
        raise ValueError(policy)
    record = {"policy": policy, "observations": [], "initial_answer": None, "released_answer": None,
              "release_actions": None, "actions": {}, "completed": False, "exception": None,
              "events": [], "answer_updates": []}
    try:
        def read(stage):
            obs = collect(source, stage, times[stage])
            record["observations"].append(obs)
            record["events"].append({"stage": stage, "event": "observed"})
            if policy == "legacy":
                _legacy_read_guard(obs)
            return obs

        initial = read("initial")
        first = {k: _render(initial.get(k), policy) for k in (ROOT, HUMIDITY)}
        record["initial_answer"] = first
        released = read("release")
        answer = {}
        for key in (ROOT, HUMIDITY):
            a, b = initial.get(key), released.get(key)
            target = "cooling" if key == ROOT else "fan"
            profile = "root-zone-status-v1" if key == ROOT else "humidity-fan-control-v1"
            diff = compare_related(_snapshot(initial, target, 0), _snapshot(released, target, 1),
                                   related_state_spec(profile, registry))
            changed = any(c.key == key for c in diff.changes) or (policy == "guarded" and a.status != b.status)
            if changed:
                answer[key] = _render(b, policy, a)
                record["answer_updates"].append(key)
            else:
                answer[key] = first[key]
        record["released_answer"] = answer
        plans = {target: propose(released, target, policy, registry) for target in ACTION_KEYS}
        record["release_actions"] = plans
        record["events"].append({"stage": "release", "event": "fixture_approval", "targets": list(plans)})
        execution = read("execution")
        for target, plan in plans.items():
            if plan["status"] != "ready":
                # Recovery cannot silently restore an unapproved held action.
                record["actions"][target] = dict(plan)
                continue
            blocked = _blocked(execution, target, policy)
            if blocked:
                record["actions"][target] = _hold(target, blocked)
                continue
            result = DryRunExecutor().execute(plan["candidate"], _snapshot(execution, target, 2), enforce_preconditions=True)
            record["actions"][target] = {**plan, "executor_reached": True, "execution": result,
                "applied": result.applied, "status": "applied" if result.applied else "hold",
                "reason": None if result.applied else "precondition_failed"}
        record["completed"] = True
    except (SnapshotUnavailable, FieldReadError) as error:
        # Runner observes termination; no invented user-facing safe hold.
        record["exception"] = {"type": type(error).__name__, "message": str(error)}
    return record
