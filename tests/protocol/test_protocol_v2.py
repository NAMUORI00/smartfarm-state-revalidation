from __future__ import annotations

import json
from pathlib import Path

import pytest

from smartfarm_state_revalidation.experiment import related_state_spec, run_suite


PROTOCOLS = Path(__file__).resolve().parents[2] / "protocols"


def load(name: str) -> dict:
    return json.loads((PROTOCOLS / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def protocol() -> dict:
    return load("protocol.v2.json")


@pytest.fixture(scope="module")
def related_v2() -> dict:
    return load("related-state.v2.json")


# --- v1 을 건드리지 않았는지 --------------------------------------------------


def test_v1_protocol_stays_frozen_and_separate() -> None:
    v1 = load("protocol.v1.json")

    assert v1["protocol_id"] == "masters-thesis-state-revalidation-v1"
    assert v1["protocol_frozen"] is True
    assert [item["condition_id"] for item in v1["conditions"]] == ["B0", "B1", "B2"]


def test_v1_related_state_declares_no_answer_units() -> None:
    v1 = load("related-state.v1.json")

    assert all("answer_units" not in profile for profile in v1["dependency_profiles"])


def test_v1_profiles_still_fall_back_to_one_unit_per_state_key() -> None:
    v1 = load("related-state.v1.json")
    spec = related_state_spec("root-zone-cooling-control-v1", v1)

    assert spec.unit_specs == ()
    assert len(spec.answer_units()) == len(spec.keys)


# --- v2 선언 ------------------------------------------------------------------


def test_v2_adds_the_selective_revision_condition(protocol: dict) -> None:
    conditions = {item["condition_id"]: item for item in protocol["conditions"]}

    assert set(conditions) == {"B0", "B1", "B2", "SR"}
    sr = conditions["SR"]
    assert sr["revision_scope"] == "affected_answer_units_only"
    assert sr["answer_refresh_limit"] == 1
    assert sr["execution_precondition_check"] is True


def test_v2_is_frozen_with_a_dated_stamp_and_base_commit(protocol: dict) -> None:
    assert protocol["protocol_frozen"] is True
    assert protocol["protocol_status"] == "frozen"
    assert protocol["frozen_at"].endswith("Z")
    assert len(protocol["implementation_base_commit"]) == 40


def test_v2_freeze_requires_a_passed_pilot(protocol: dict) -> None:
    gate = protocol["pilot_gate"]

    assert gate["status"] == "passed"
    assert gate["claim_status"] == "PILOT_ONLY"
    assert gate["protocol_error_count"] == 0
    assert gate["artifact_reaggregation"] == "ok"


def test_v2_reuses_the_frozen_v1_confirmatory_scenarios(protocol: dict) -> None:
    """같은 확정 시나리오를 써야 v1 결과와 직접 비교할 수 있다."""
    v1 = load("protocol.v1.json")

    assert protocol["frozen_inputs"]["scenario_sha256"] == v1["frozen_inputs"]["scenario_sha256"]
    assert protocol["frozen_inputs"]["scenario_count"] == 120
    # 관련 상태만 달라진다. 답변 단위를 새로 선언했기 때문이다.
    assert (
        protocol["frozen_inputs"]["related_state_sha256"]
        != v1["frozen_inputs"]["related_state_sha256"]
    )
    assert protocol["scenario_suite_source"]["path"].endswith("scenarios.confirmatory.v1.json")


def test_v2_keeps_token_and_latency_unclaimed(protocol: dict) -> None:
    scope = protocol["claim_scope"]

    assert "llm_calls_tokens_and_cost" in scope["not_claimed_metrics"]
    assert "response_latency_ms" in scope["not_claimed_metrics"]
    assert "unit_generation_count" in scope["claimed_metrics"]
    assert "structural_proxy" in scope["cost_interpretation"]


def test_v2_declares_the_selective_revision_contrast(protocol: dict) -> None:
    contrasts = {item["contrast_id"] for item in protocol["primary_contrasts"]}

    assert {"SR-B0", "SR-B2"} <= contrasts


def test_answer_unit_rule_forbids_depending_on_generated_output(protocol: dict) -> None:
    rule = protocol["answer_unit_rule"]

    assert rule["declaration_source"] == "related_state_dependency_profile"
    assert "generated_answer" in rule["must_not_depend_on"]
    assert "generated_state_claims" in rule["must_not_depend_on"]


# --- v2 단위 선언의 정합성 ----------------------------------------------------


def test_every_profile_partitions_its_required_state_keys(related_v2: dict) -> None:
    for profile in related_v2["dependency_profiles"]:
        required = set(profile["required_state_keys"])
        declared = [key for unit in profile["answer_units"] for key in unit["depends_on"]]

        assert set(declared) == required
        assert len(declared) == len(required), "상태키가 두 단위에 중복되면 안 된다"


def test_v2_profiles_load_into_declared_units(related_v2: dict) -> None:
    spec = related_state_spec("root-zone-cooling-control-v1", related_v2)

    assert [unit.unit_id for unit in spec.answer_units()] == [
        "sensor_reading",
        "alert_status",
        "device_status",
        "data_quality",
    ]


# --- v2 실행 ------------------------------------------------------------------


def test_selective_revision_generates_fewer_units_than_full_refresh(
    related_v2: dict,
) -> None:
    suite = load("scenarios.v1.json")

    records = run_suite(
        suite,
        related_v2,
        conditions=("B2", "SR"),
        diagnostic_conditions=("B2", "SR"),
    )
    by_condition: dict[str, list[dict]] = {}
    for record in records:
        by_condition.setdefault(record["condition_id"], []).append(record)

    b2_units = sum(r["answer_units"]["generated_total"] for r in by_condition["B2"])
    sr_units = sum(r["answer_units"]["generated_total"] for r in by_condition["SR"])

    assert sr_units < b2_units


def test_selective_revision_keeps_the_same_final_state_claims(related_v2: dict) -> None:
    suite = load("scenarios.v1.json")

    records = run_suite(
        suite,
        related_v2,
        conditions=("B2", "SR"),
        diagnostic_conditions=("B2", "SR"),
    )

    def claims_by_pair(condition: str) -> dict[str, dict[str, object]]:
        return {
            record["pair_id"]: {
                item["key"]: item["value"] for item in record["response"]["claims"]
            }
            for record in records
            if record["condition_id"] == condition
        }

    b2, sr = claims_by_pair("B2"), claims_by_pair("SR")
    shared = set(b2) & set(sr)

    assert shared, "비교할 짝이 있어야 한다"
    assert all(b2[pair_id] == sr[pair_id] for pair_id in shared)


def test_diagnostic_scope_defaults_to_v1_behaviour(related_v2: dict) -> None:
    suite = load("scenarios.v1.json")

    records = run_suite(suite, related_v2, conditions=("B0", "B1", "B2", "SR"))
    diagnostic = [
        record
        for record in records
        if record["comparison_scope"] == "B2_safety_diagnostic"
    ]

    assert diagnostic, "진단 시나리오가 있어야 한다"
    assert {record["condition_id"] for record in diagnostic} == {"B2"}
