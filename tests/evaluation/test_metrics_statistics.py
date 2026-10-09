from __future__ import annotations

import pytest

from smartfarm_state_revalidation.evaluation.metrics import aggregate_metrics
from smartfarm_state_revalidation.evaluation.statistics import (
    paired_binary_exact,
    paired_difference_bootstrap,
    wilson_interval,
    zero_event_upper_bound,
)


def test_zero_denominators_are_null_and_not_applicable() -> None:
    summary = aggregate_metrics([])

    for name in (
        "response_state_consistency",
        "invalid_action_execution_rate",
        "valid_action_retention",
        "unnecessary_refresh_rate",
        "change_detection_rate",
        "second_change_bounded_termination_rate",
    ):
        metric = summary["metrics"][name]
        assert metric["status"] == "not_applicable"
        assert metric["denominator"] == 0
        assert metric["value"] is None
        assert metric["interval"]["lower"] is None


def test_metrics_keep_explicit_numerators_denominators_calls_and_latency() -> None:
    records = [
        {
            "response_claim_consistent": False,
            "response_claim_consistent_count": 1,
            "response_claim_count": 2,
            "invalid_action_opportunity": True,
            "valid_action_opportunity": False,
            "action_valid": False,
            "executed": True,
            "refresh_required": True,
            "refresh_performed": False,
            "change_expected": True,
            "change_detected": False,
            "calls": {"state_snapshot": 1},
            "latencies_ms": {"revalidation": 2.0},
        },
        {
            "response_claim_consistent": True,
            "response_claim_consistent_count": 2,
            "response_claim_count": 2,
            "invalid_action_opportunity": False,
            "valid_action_opportunity": True,
            "action_valid": True,
            "candidate_retained": True,
            "executed": True,
            "refresh_required": False,
            "refresh_performed": True,
            "change_expected": False,
            "change_detected": None,
            "calls": {"state_snapshot": 3},
            "latencies_ms": {"revalidation": 6.0},
        },
    ]

    summary = aggregate_metrics(records)

    consistency = summary["metrics"]["response_state_consistency"]
    assert consistency["numerator"] == 1
    assert consistency["denominator"] == 2
    assert consistency["value"] == 0.5
    field_agreement = summary["metrics"]["required_field_agreement"]
    assert field_agreement["numerator"] == 3
    assert field_agreement["denominator"] == 4
    assert field_agreement["value"] == 0.75
    assert summary["metrics"]["invalid_action_execution_rate"]["value"] == 1.0
    assert summary["metrics"]["valid_action_retention"]["value"] == 1.0
    assert summary["metrics"]["unnecessary_refresh_rate"]["value"] == 1.0
    assert summary["metrics"]["change_detection_rate"]["value"] == 0.0
    assert summary["metrics"]["calls.state_snapshot.per_episode"]["numerator"] == 4.0
    assert summary["metrics"]["calls.state_snapshot.per_episode"]["denominator"] == 2
    assert summary["metrics"]["latency.revalidation_ms"]["value"] == 4.0
    assert summary["metrics"]["latency.revalidation_ms"]["p95"] == 6.0


def test_wilson_interval_reports_uncertainty_for_zero_events() -> None:
    interval = wilson_interval(0, 10)

    assert interval["status"] == "ok"
    assert interval["lower"] == pytest.approx(0.0)
    assert interval["upper"] > 0.0
    exact = zero_event_upper_bound(10)
    assert exact["status"] == "ok"
    assert exact["upper"] == pytest.approx(1 - 0.05 ** (1 / 10))


def test_paired_bootstrap_and_exact_binary_statistics_are_deterministic() -> None:
    baseline = [0, 0, 1, 0]
    treatment = [1, 0, 1, 1]

    first = paired_difference_bootstrap(baseline, treatment, seed=41, resamples=2_000)
    second = paired_difference_bootstrap(baseline, treatment, seed=41, resamples=2_000)
    exact = paired_binary_exact(baseline, treatment)

    assert first == second
    assert first["difference"] == pytest.approx(0.5)
    assert first["paired_count"] == 4
    assert exact["improved"] == 2
    assert exact["worsened"] == 0
    assert exact["p_value_two_sided"] == pytest.approx(0.5)


def test_paired_statistics_do_not_replace_missing_pairs_with_zero() -> None:
    result = paired_difference_bootstrap([None], [True], resamples=10)

    assert result["status"] == "not_applicable"
    assert result["paired_count"] == 0
    assert result["difference"] is None
