from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_confirmatory_result_index_preserves_scope_and_frozen_inputs() -> None:
    result = _load(ROOT / "results" / "confirmatory-v1.json")
    protocol = _load(ROOT / "protocols" / "protocol.v1.json")

    assert result["claim_status"] == "claim_candidate"
    assert result["evidence_scope"] == "controlled_simulated_scenario_suite_only"
    assert result["run"]["scenario_count"] == protocol["frozen_inputs"]["scenario_count"]
    assert result["inputs"]["scenario_sha256"] == protocol["frozen_inputs"]["scenario_sha256"]
    assert (
        result["inputs"]["related_state_sha256"]
        == protocol["frozen_inputs"]["related_state_sha256"]
    )
    assert result["integrity"]["artifact_hash_verification"] == "ok"
    assert result["integrity"]["summary_reaggregation"] == "ok"
    assert result["integrity"]["statistics_reaggregation"] == "ok"
    assert result["integrity"]["protocol_error_count"] == 0
    assert result["integrity"]["oracle_state_mismatch_count"] == 0
    assert {
        "actual_llm_or_rag_natural_language_quality",
        "response_latency",
        "llm_calls_tokens_and_cost",
        "real_farm_safety_or_productivity",
        "physical_device_effect",
        "population_level_generalization",
    } <= set(result["not_claimed"])
    assert result["preclaim_audit_disclosure"]["prior_nonclaim_execution_observed"] is True


def test_confirmatory_result_index_rates_match_recorded_counts() -> None:
    result = _load(ROOT / "results" / "confirmatory-v1.json")
    for metrics in result["primary_metrics"].values():
        for metric in metrics.values():
            assert metric["denominator"] > 0
            assert metric["value"] == metric["numerator"] / metric["denominator"]
    diagnostic = result["diagnostic_metrics"]["B2_second_change_bounded_termination_rate"]
    assert diagnostic["value"] == diagnostic["numerator"] / diagnostic["denominator"]

    assert result["primary_metrics"]["B2"]["response_state_consistency"]["value"] == 1.0
    assert result["primary_metrics"]["B2"]["invalid_action_execution_rate"]["value"] == 0.0
    assert result["primary_metrics"]["B2"]["valid_action_retention"]["value"] == 1.0
