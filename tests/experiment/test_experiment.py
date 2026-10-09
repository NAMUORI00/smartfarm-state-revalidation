from __future__ import annotations

from pathlib import Path

from smartfarm_state_revalidation.evaluation.runner import evaluate_records
from smartfarm_state_revalidation.evaluation.artifacts import _claim_ledger_rows, _metric_rows
from smartfarm_state_revalidation.experiment import evaluation_protocol, load_json, run_suite


ROOT = Path(__file__).resolve().parents[2]


def _inputs() -> tuple[dict, dict, dict]:
    protocol = load_json(ROOT / "protocols" / "protocol.v1.json")
    scenarios = load_json(ROOT / "protocols" / "scenarios.v1.json")
    related = load_json(ROOT / "protocols" / "related-state.v1.json")
    return protocol, scenarios, related


def test_full_suite_has_paired_b0_b1_b2_records() -> None:
    _protocol, scenarios, related = _inputs()
    records = run_suite(scenarios, related)

    assert len(records) == 34
    assert {record["condition_id"] for record in records} == {"B0", "B1", "B2"}
    assert all(
        {record["condition_id"] for record in records if record["pair_id"] == pair_id}
        == {"B0", "B1", "B2"}
        for pair_id in {
            record["pair_id"]
            for record in records
            if record["comparison_scope"] == "paired_primary"
        }
    )
    diagnostic = [record for record in records if record["comparison_scope"] != "paired_primary"]
    assert len(diagnostic) == 1
    assert diagnostic[0]["condition_id"] == "B2"


def test_controlled_suite_separates_blocking_from_bounded_refresh() -> None:
    protocol, scenarios, related = _inputs()
    evaluated = evaluate_records(
        run_suite(scenarios, related),
        evaluation_protocol(protocol, related),
        comparisons=(("B0", "B1"), ("B1", "B2"), ("B0", "B2")),
        bootstrap_seed=7,
        bootstrap_resamples=500,
    )
    conditions = evaluated["summary"]["conditions"]

    assert conditions["B0"]["metrics"]["response_state_consistency"]["numerator"] == 5
    assert conditions["B1"]["metrics"]["response_state_consistency"]["numerator"] == 5
    assert conditions["B2"]["metrics"]["response_state_consistency"]["numerator"] == 11
    assert conditions["B0"]["metrics"]["invalid_action_execution_rate"]["numerator"] == 8
    assert conditions["B1"]["metrics"]["invalid_action_execution_rate"]["numerator"] == 0
    assert conditions["B2"]["metrics"]["invalid_action_execution_rate"]["numerator"] == 0
    assert conditions["B1"]["metrics"]["valid_action_retention"]["numerator"] == 3
    assert conditions["B2"]["metrics"]["valid_action_retention"]["numerator"] == 5
    response_effect = evaluated["statistics"]["comparisons"]["B2_minus_B0"]["metrics"][
        "response_state_consistency"
    ]["bootstrap"]
    assert response_effect["paired_count"] == 11
    assert response_effect["difference"] == 6 / 11
    zero_event_interval = conditions["B2"]["metrics"]["invalid_action_execution_rate"][
        "interval"
    ]
    assert zero_event_interval["zero_event_upper"]["upper"] > 0.0
    diagnostic = evaluated["summary"]["diagnostics"]["metrics"][
        "second_change_bounded_termination_rate"
    ]
    assert diagnostic["numerator"] == 1
    assert diagnostic["denominator"] == 1

    metric_rows = _metric_rows(evaluated["summary"])
    second_change_row = next(
        row
        for row in metric_rows
        if row["condition_id"] == "B2_diagnostic"
        and row["metric_id"] == "second_change_bounded_termination_rate"
    )
    assert second_change_row["numerator"] == 1
    ledger = _claim_ledger_rows(
        metric_rows,
        claim_status="not_claimed",
        summary_sha256="0" * 64,
        claimed_metrics={
            "response_state_consistency",
            "invalid_action_execution_rate",
            "valid_action_retention",
            "unnecessary_refresh_rate",
            "second_change_bounded_termination_rate",
        },
    )
    assert any(row["claim_id"] == "B2_diagnostic:second_change_bounded_termination_rate" for row in ledger)
    assert all(not str(row["metric_id"]).startswith("calls.") for row in ledger)


def test_second_refresh_change_returns_latest_claims_without_action() -> None:
    _protocol, scenarios, related = _inputs()
    record = next(
        item
        for item in run_suite(scenarios, related, conditions=("B2",))
        if item["episode_id"] == "S09-second-change-during-refresh"
    )

    assert record["failure_code"] == "refresh_exhausted"
    assert record["outcome"]["candidate_retained"] is False
    assert record["outcome"]["executed"] is False
    assert record["source_revision"] == 92
    assert record["response"]["claims"]
    assert all(claim["observed_revision"] == 92 for claim in record["response"]["claims"])


def test_execution_boundary_changes_are_blocked_only_by_checked_policies() -> None:
    _protocol, scenarios, related = _inputs()
    records = run_suite(scenarios, related)

    for episode_id in ("S11-change-before-execution-check", "S12-change-at-action-apply"):
        by_condition = {
            record["condition_id"]: record
            for record in records
            if record["episode_id"] == episode_id
        }
        assert by_condition["B0"]["outcome"]["executed"] is True
        assert by_condition["B1"]["outcome"]["executed"] is False
        assert by_condition["B2"]["outcome"]["executed"] is False


def test_logical_results_are_reproducible_without_wall_clock_latency() -> None:
    _protocol, scenarios, related = _inputs()
    first = run_suite(scenarios, related)
    second = run_suite(scenarios, related)

    for records in (first, second):
        for record in records:
            record["latencies_ms"] = {}
    assert first == second
