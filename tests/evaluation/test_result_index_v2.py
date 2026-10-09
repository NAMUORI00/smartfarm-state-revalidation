from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _index() -> dict:
    return _load(ROOT / "results" / "confirmatory-v2.json")


def test_index_matches_the_frozen_v2_protocol_inputs() -> None:
    result = _index()
    protocol = _load(ROOT / "protocols" / "protocol.v2.json")

    assert result["claim_status"] == "claim_candidate"
    assert result["run"]["scenario_count"] == protocol["frozen_inputs"]["scenario_count"]
    assert result["inputs"]["scenario_sha256"] == protocol["frozen_inputs"]["scenario_sha256"]
    assert (
        result["inputs"]["related_state_sha256"]
        == protocol["frozen_inputs"]["related_state_sha256"]
    )
    assert result["run"]["condition_ids"] == [
        item["condition_id"] for item in protocol["conditions"]
    ]


def test_index_shares_the_confirmatory_scenarios_with_v1() -> None:
    result = _index()
    v1 = _load(ROOT / "results" / "confirmatory-v1.json")

    assert result["extends"] == v1["result_id"]
    assert result["inputs"]["scenario_sha256"] == v1["inputs"]["scenario_sha256"]
    assert result["inputs"]["scenario_suite_shared_with"] == "confirmatory-v1"


def test_index_reports_a_clean_run() -> None:
    integrity = _index()["integrity"]

    assert integrity["artifact_hash_verification"] == "ok"
    assert integrity["summary_reaggregation"] == "ok"
    assert integrity["statistics_reaggregation"] == "ok"
    assert integrity["protocol_error_count"] == 0
    assert integrity["oracle_state_mismatch_count"] == 0


def test_selective_revision_matches_full_refresh_on_every_correctness_metric() -> None:
    effects = _index()["paired_effects"]

    for metric in (
        "response_state_consistency",
        "valid_action_retention",
        "invalid_action_execution",
    ):
        effect = effects[f"SR_minus_B2_{metric}"]
        lower, upper = effect["paired_bootstrap_95"]
        assert effect["difference"] == 0.0
        assert lower == 0.0 and upper == 0.0


def test_selective_revision_costs_strictly_fewer_units_than_full_refresh() -> None:
    effect = _index()["paired_effects"]["SR_minus_B2_unit_generation_count"]
    lower, upper = effect["paired_bootstrap_95"]

    assert effect["difference"] < 0
    assert upper < 0, "95% 구간이 0 을 포함하면 비용 절감을 주장할 수 없다"
    assert effect["higher_is_better"] is False


def test_selective_revision_adds_less_overhead_than_full_refresh() -> None:
    effects = _index()["paired_effects"]
    selective = effects["SR_minus_B0_unit_generation_count"]["difference"]
    full = effects["B2_minus_B0_unit_generation_count"]["difference"]

    assert 0 < selective < full


def test_revision_scope_is_precise_only_under_selective_revision() -> None:
    metrics = _index()["primary_metrics"]

    assert metrics["SR"]["revision_precision"]["value"] == 1.0
    assert metrics["SR"]["revision_recall"]["value"] == 1.0
    assert metrics["B2"]["revision_precision"]["value"] < 1.0
    assert metrics["B2"]["revision_recall"]["value"] == 1.0


def test_preserved_units_only_exist_under_selective_revision() -> None:
    metrics = _index()["primary_metrics"]

    assert metrics["SR"]["preserved_unit_ratio"]["value"] > 0
    assert metrics["B2"]["preserved_unit_ratio"]["value"] == 0.0


def test_rates_match_their_recorded_counts() -> None:
    for condition, metrics in _index()["primary_metrics"].items():
        for name, block in metrics.items():
            if "numerator" not in block:
                continue
            denominator = block["denominator"]
            expected = block["numerator"] / denominator if denominator else None
            assert block["value"] == expected, f"{condition}.{name}"


def test_index_keeps_token_latency_and_farm_effects_unclaimed() -> None:
    result = _index()

    assert {
        "response_latency",
        "llm_calls_tokens_and_cost",
        "real_farm_safety_or_productivity",
        "physical_device_effect",
    } <= set(result["not_claimed"])
    assert "structural_proxy" in result["cost_interpretation"]
