"""Metric aggregation with explicit numerators, denominators, and applicability."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from math import ceil
from statistics import fmean, median
from typing import Any, Iterable, Mapping

from .statistics import wilson_interval, zero_event_upper_bound


@dataclass(frozen=True)
class MetricResult:
    name: str
    status: str
    numerator: int | float
    denominator: int
    value: float | None
    unit: str
    interval: Mapping[str, Any] | None = None
    median: float | None = None
    p95: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _rate(name: str, numerator: int, denominator: int) -> MetricResult:
    interval = wilson_interval(numerator, denominator)
    if numerator == 0 and denominator > 0:
        interval = {**interval, "zero_event_upper": zero_event_upper_bound(denominator)}
    return MetricResult(
        name=name,
        status="ok" if denominator else "not_applicable",
        numerator=numerator,
        denominator=denominator,
        value=numerator / denominator if denominator else None,
        unit="proportion",
        interval=interval,
    )


def _mean(name: str, values: list[float], unit: str) -> MetricResult:
    if not values:
        return MetricResult(name, "not_applicable", 0.0, 0, None, unit)
    ordered = sorted(values)
    p95_index = max(0, ceil(0.95 * len(ordered)) - 1)
    return MetricResult(
        name=name,
        status="ok",
        numerator=sum(values),
        denominator=len(values),
        value=fmean(values),
        unit=unit,
        median=median(values),
        p95=ordered[p95_index],
    )


def aggregate_metrics(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate normalized episode records without assigning values to missing cases."""

    rows = list(records)
    episode_consistency_rows = [
        row for row in rows if isinstance(row.get("response_claim_consistent"), bool)
    ]
    episode_consistency_successes = sum(
        row.get("response_claim_consistent") is True for row in episode_consistency_rows
    )
    claim_denominator = sum(int(row.get("response_claim_count", 0)) for row in rows)
    claim_numerator = sum(int(row.get("response_claim_consistent_count", 0)) for row in rows)

    invalid_rows = [
        row
        for row in rows
        if row.get("invalid_action_opportunity") is True
    ]
    invalid_executions = sum(
        row.get("executed") is True and row.get("action_valid") is False for row in invalid_rows
    )
    valid_rows = [
        row
        for row in rows
        if row.get("valid_action_opportunity") is True
    ]
    valid_retained = sum(
        row.get("candidate_retained") is True and row.get("action_valid") is True
        for row in valid_rows
    )
    no_refresh_rows = [
        row
        for row in rows
        if row.get("refresh_required") is False and isinstance(row.get("refresh_performed"), bool)
    ]
    unnecessary_refreshes = sum(row.get("refresh_performed") is True for row in no_refresh_rows)
    changed_rows = [
        row
        for row in rows
        if row.get("change_expected") is True and isinstance(row.get("change_detected"), bool)
    ]
    detected_changes = sum(row.get("change_detected") is True for row in changed_rows)
    second_change_rows = [
        row for row in rows if row.get("scenario_category") == "second_change_during_refresh"
    ]
    # refresh_exhausted 는 갱신 예산이 있는 조건에서만 나온다.
    # 조건 이름을 직접 검사하지 않아야 v2 의 SR 도 같은 진단에 포함된다.
    bounded_terminations = sum(
        row.get("failure_code") == "refresh_exhausted"
        and row.get("candidate_retained") is False
        and row.get("executed") is False
        and isinstance(row.get("calls"), Mapping)
        and row["calls"].get("generation", 0) <= 2
        for row in second_change_rows
    )

    # --- 답변 단위 회계 -------------------------------------------------------
    # 갱신이 실제로 일어난 에피소드에서만 선택적 수정의 효율을 잰다.
    # 갱신 예산이 없는 조건은 분모가 0 이므로 not_applicable 로 남는다.
    unit_rows = [row for row in rows if isinstance(row.get("answer_units"), Mapping)]
    refreshed_unit_rows = [
        row for row in unit_rows if row.get("refresh_performed") is True
    ]
    declared_units = sum(
        int(row["answer_units"].get("declared", 0)) for row in refreshed_unit_rows
    )
    preserved_units = sum(
        int(row["answer_units"].get("preserved", 0)) for row in refreshed_unit_rows
    )
    revised_total = 0
    revised_correct = 0
    oracle_total = 0
    oracle_covered = 0
    for row in refreshed_unit_rows:
        units = row["answer_units"]
        revised = set(units.get("revised_unit_ids", ()))
        oracle = set(units.get("oracle_affected_unit_ids", ()))
        revised_total += len(revised)
        revised_correct += len(revised & oracle)
        oracle_total += len(oracle)
        oracle_covered += len(revised & oracle)

    unit_generation_values = [
        float(row["answer_units"].get("generated_total", 0)) for row in unit_rows
    ]

    metrics: dict[str, MetricResult] = {
        "preserved_unit_ratio": _rate(
            "preserved_unit_ratio", preserved_units, declared_units
        ),
        "revision_precision": _rate(
            "revision_precision", revised_correct, revised_total
        ),
        "revision_recall": _rate("revision_recall", oracle_covered, oracle_total),
        "unit_generation_count": _mean(
            "unit_generation_count", unit_generation_values, "answer_units"
        ),
        "response_state_consistency": _rate(
            "response_state_consistency",
            episode_consistency_successes,
            len(episode_consistency_rows),
        ),
        "required_field_agreement": _rate(
            "required_field_agreement", claim_numerator, claim_denominator
        ),
        "invalid_action_execution_rate": _rate(
            "invalid_action_execution_rate", invalid_executions, len(invalid_rows)
        ),
        "valid_action_retention": _rate(
            "valid_action_retention", valid_retained, len(valid_rows)
        ),
        "unnecessary_refresh_rate": _rate(
            "unnecessary_refresh_rate", unnecessary_refreshes, len(no_refresh_rows)
        ),
        "change_detection_rate": _rate(
            "change_detection_rate", detected_changes, len(changed_rows)
        ),
        "second_change_bounded_termination_rate": _rate(
            "second_change_bounded_termination_rate",
            bounded_terminations,
            len(second_change_rows),
        ),
    }

    call_names = sorted(
        {
            str(name)
            for row in rows
            for name in row.get("calls", {})
            if isinstance(row.get("calls"), Mapping)
        }
    )
    for name in call_names:
        values = [
            float(row["calls"][name])
            for row in rows
            if isinstance(row.get("calls"), Mapping) and isinstance(row["calls"].get(name), (int, float))
        ]
        metrics[f"calls.{name}.per_episode"] = _mean(f"calls.{name}.per_episode", values, "calls")

    latency_names = sorted(
        {
            str(name)
            for row in rows
            for name in row.get("latencies_ms", {})
            if isinstance(row.get("latencies_ms"), Mapping)
        }
    )
    for name in latency_names:
        values = [
            float(row["latencies_ms"][name])
            for row in rows
            if isinstance(row.get("latencies_ms"), Mapping)
            and isinstance(row["latencies_ms"].get(name), (int, float))
        ]
        metrics[f"latency.{name}_ms"] = _mean(f"latency.{name}_ms", values, "milliseconds")

    return {
        "episode_count": len(rows),
        "protocol_error_count": sum(int(row.get("protocol_error_count", 0)) for row in rows),
        "metrics": {name: metric.to_dict() for name, metric in sorted(metrics.items())},
    }
