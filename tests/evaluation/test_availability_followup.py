from copy import deepcopy
import json

import pytest

from smartfarm_state_revalidation.evaluation.availability_followup import (
    RESEARCH, ROOT, execute, judge, summarize,
)


@pytest.fixture(scope="module")
def measured():
    protocol = json.loads((RESEARCH / "protocols/availability-followup.v1.json").read_text(encoding="utf-8"))
    # Runner unit test uses one case per condition. Published run uses all ten.
    protocol["variants"] = protocol["variants"][:1]
    registry = json.loads((RESEARCH / "protocols/related-state.v2.json").read_text(encoding="utf-8"))
    return protocol, execute(protocol, registry)


def test_exception_remains_in_denominator_and_is_not_a_safe_hold(measured):
    protocol, cases = measured
    rows = summarize(cases, protocol)
    for row in rows[2:]:
        assert row["N"] == row["exceptions"] == 1
        assert row["metrics"]["S"]["passed"] == 0
        assert row["metrics"]["C"]["passed"] == 0


def test_oracle_rejects_wrong_temperature_and_does_not_trust_valid_flag(measured):
    protocol, cases = measured
    original = cases[0]
    condition = protocol["conditions"][0]
    assert original["scores"] == {"A": 1, "C": 1, "F": 1, "S": 1}
    mutated = deepcopy(original)
    for claim in mutated["probes"]["cooling"]["released_response"]["state_claims"]:
        if claim["key"] == ROOT:
            claim["value"] = 99
    assert judge(mutated, condition, protocol["oracle"])["A"] == 0
    mutated = deepcopy(original)
    mutated["probes"]["cooling"]["executor_calls"][-1]["state"]["values"][ROOT] = 17
    assert judge(mutated, condition, protocol["oracle"])["C"] == 0


def test_oracle_rejects_changed_fan_candidate(measured):
    protocol, cases = measured
    mutated = deepcopy(cases[0])
    mutated["probes"]["fan"]["executor_calls"][-1]["candidate"]["value"] = 80
    assert judge(mutated, protocol["conditions"][0], protocol["oracle"])["F"] == 0


def test_missing_none_is_not_an_unavailable_explanation(measured):
    protocol, cases = measured
    case = cases[1]
    assert case["probes"]["cooling"]["release_changed"] is False
    assert case["scores"] == {"A": 0, "C": 0, "F": 1, "S": 0}
    # An explicit unavailable notice and reason, if implemented, can pass.
    changed = deepcopy(case)
    changed["probes"]["cooling"]["released_response"]["text"] = "현재 온도 확인 불가"
    changed["probes"]["cooling"]["executor_calls"][-1]["result"]["failure_code"] = "required_value_missing"
    assert judge(changed, protocol["conditions"][1], protocol["oracle"])["S"] == 1


def test_temperature_error_is_distinct_from_whole_snapshot_failure(measured):
    _, cases = measured
    partial = cases[2]["probes"]["fan"]["reads"][1]
    whole = cases[3]["probes"]["fan"]["reads"][1]
    assert partial["fault_source"] == ROOT
    assert partial["independent_source_values_available"]
    assert whole["fault_source"] == "all_state"
    assert not whole["independent_source_values_available"]
    assert cases[2]["probes"]["cooling"]["released_response"] is None


def test_manifest_conditions_have_unique_paired_variants():
    protocol = json.loads((RESEARCH / "protocols/availability-followup.v1.json").read_text(encoding="utf-8"))
    assert len({v["id"] for v in protocol["variants"]}) == 10
    assert len({(v["initial_fan_percent"], v["sample_age_seconds"]) for v in protocol["variants"]}) == 10
    assert sum(c["primary"] for c in protocol["conditions"]) == 3
