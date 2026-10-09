from __future__ import annotations

from smartfarm_state_revalidation.evaluation.oracle import (
    detect_related_changes,
    evaluate_episode,
    evaluate_preconditions,
    evaluate_response_claims,
)


PROTOCOL = {
    "state_keys": [
        {
            "key": "sensor.zone-a.temperature_c",
            "kind": "numeric_sensor",
            "absolute_epsilon": 0.1,
            "semantic_bands": [
                {"label": "normal", "upper_inclusive": 20.0},
                {"label": "high", "lower_exclusive": 20.0},
            ],
        },
        {
            "key": "alert.zone-a.high.active",
            "kind": "boolean_alert_state",
            "comparison": "exact",
        },
    ],
    "dependency_profiles": [
        {
            "profile_id": "cooling-v1",
            "required_state_keys": [
                "alert.zone-a.high.active",
                "sensor.zone-a.temperature_c",
            ],
        }
    ],
}


def test_structured_claims_and_preconditions_are_evaluated_without_a_judge() -> None:
    state = {
        "sensor.zone-a.temperature_c": 22.0,
        "device": {"zone-a": {"cooling": {"enabled": False}}},
    }
    claims = [
        {"key": "sensor.zone-a.temperature_c", "operator": "gte", "value": 20.0},
        {"key": "device.zone-a.cooling.enabled", "operator": "eq", "value": False},
    ]
    preconditions = [
        {"key": "sensor.zone-a.temperature_c", "operator": "between", "value": {"lower": 20, "upper": 30}},
        {"key": "device.zone-a.cooling.enabled", "operator": "ne", "value": True},
    ]

    claim_result = evaluate_response_claims(claims, state)
    action_result = evaluate_preconditions(preconditions, state)

    assert claim_result.status == "satisfied"
    assert claim_result.numerator == 2
    assert claim_result.denominator == 2
    assert action_result.satisfied is True


def test_empty_claims_are_not_applicable_and_protocol_errors_are_not_scored_as_zero() -> None:
    empty = evaluate_response_claims([], {})
    broken = evaluate_response_claims(
        [{"key": "sensor.value", "operator": "unsupported", "value": 1}],
        {"sensor.value": 1},
    )

    assert empty.status == "not_applicable"
    assert empty.satisfied is None
    assert empty.denominator == 0
    assert broken.status == "protocol_error"
    assert broken.satisfied is None
    assert broken.error_count == 1


def test_related_change_uses_declared_tolerance_band_and_exact_state() -> None:
    within_tolerance = detect_related_changes(
        {"sensor.zone-a.temperature_c": 19.95, "alert.zone-a.high.active": False},
        {"sensor.zone-a.temperature_c": 20.00, "alert.zone-a.high.active": False},
        ["sensor.zone-a.temperature_c", "alert.zone-a.high.active"],
        PROTOCOL,
    )
    crossed_band = detect_related_changes(
        {"sensor.zone-a.temperature_c": 19.95, "alert.zone-a.high.active": False},
        {"sensor.zone-a.temperature_c": 20.01, "alert.zone-a.high.active": True},
        ["sensor.zone-a.temperature_c", "alert.zone-a.high.active"],
        PROTOCOL,
    )

    assert within_tolerance["changed"] is False
    assert crossed_band["changed"] is True
    assert crossed_band["changed_keys"] == [
        "alert.zone-a.high.active",
        "sensor.zone-a.temperature_c",
    ]


def test_episode_oracle_derives_response_consistency_action_validity_and_change_detection() -> None:
    record = {
        "episode_id": "episode-1",
        "pair_id": "episode-1",
        "condition_id": "B0",
        "dependency_profile_id": "cooling-v1",
        "states": {
            "initial": {
                "sensor.zone-a.temperature_c": 22.0,
                "alert.zone-a.high.active": True,
            },
            "response": {
                "sensor.zone-a.temperature_c": 18.0,
                "alert.zone-a.high.active": False,
            },
            "execution": {
                "sensor.zone-a.temperature_c": 18.0,
                "alert.zone-a.high.active": False,
            },
        },
        "response_claims": [
            {"key": "sensor.zone-a.temperature_c", "operator": "eq", "value": 22.0}
        ],
        "action": {
            "candidate": {
                "action": "cool",
                "target": "zone-a",
                "value": True,
                "bound_state_revision": 1,
            }
        },
        "oracle": {
            "initial_truth": {
                "sensor.zone-a.temperature_c": 22.0,
                "alert.zone-a.high.active": True,
            },
            "release_truth": {
                "sensor.zone-a.temperature_c": 18.0,
                "alert.zone-a.high.active": False,
            },
            "execution_truth": {
                "sensor.zone-a.temperature_c": 18.0,
                "alert.zone-a.high.active": False,
            },
            "initial_candidate": {
                "action": "cool",
                "target": "zone-a",
                "value": True,
                "bound_state_revision": 1,
            },
            "expected_action_candidate": None,
            "execution_preconditions": [
                {"state_key": "alert.zone-a.high.active", "operator": "eq", "expected": True}
            ],
        },
        "outcome": {
            "executed": True,
            "refresh_performed": False,
            "detected_state_keys": [],
        },
    }

    evaluated = evaluate_episode(record, PROTOCOL)

    assert evaluated["response_claim_consistent"] is False
    assert evaluated["action_valid"] is False
    assert evaluated["invalid_action_opportunity"] is True
    assert evaluated["change_expected"] is True
    assert evaluated["change_detected"] is False
    assert evaluated["refresh_required"] is True
