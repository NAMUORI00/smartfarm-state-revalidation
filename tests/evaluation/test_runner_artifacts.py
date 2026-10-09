from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import pytest

from smartfarm_state_revalidation.evaluation.artifacts import verify_artifact_bundle
from smartfarm_state_revalidation.evaluation.runner import (
    evaluate_records,
    run_evaluation,
    verify_reaggregation,
)


PROTOCOL = {
    "schema_version": "test-protocol-v1",
    "state_keys": [
        {"key": "alert.active", "kind": "boolean_alert_state", "comparison": "exact"},
        {"key": "device.enabled", "kind": "boolean_device_state", "comparison": "exact"},
    ],
    "dependency_profiles": [
        {"profile_id": "action-v1", "required_state_keys": ["alert.active", "device.enabled"]}
    ],
}


def observation(
    episode_id: str,
    condition_id: str,
    *,
    initial_alert: bool,
    final_alert: bool,
    claim_value: bool,
    executed: bool,
    refreshed: bool,
    detected: bool | None,
) -> dict:
    initial_candidate = {
        "action": "cool",
        "target": "zone-a",
        "value": True,
        "bound_state_revision": 1,
    }
    expected_candidate = initial_candidate if final_alert else None
    actual_candidate = initial_candidate if executed else None
    return {
        "episode_id": episode_id,
        "pair_id": episode_id,
        "condition_id": condition_id,
        "dependency_profile_id": "action-v1",
        "states": {
            "initial": {"alert.active": initial_alert, "device.enabled": False},
            "response": {"alert.active": final_alert, "device.enabled": False},
            "execution": {"alert.active": final_alert, "device.enabled": False},
        },
        "response_claims": [
            {"key": "alert.active", "operator": "eq", "value": claim_value},
            {"key": "device.enabled", "operator": "eq", "value": False},
        ],
        "action": {"candidate": actual_candidate},
        "oracle": {
            "initial_truth": {"alert.active": initial_alert, "device.enabled": False},
            "release_truth": {"alert.active": final_alert, "device.enabled": False},
            "execution_truth": {"alert.active": final_alert, "device.enabled": False},
            "initial_candidate": initial_candidate,
            "expected_action_candidate": expected_candidate,
            "execution_preconditions": [
                {"state_key": "alert.active", "operator": "eq", "expected": True}
            ],
        },
        "outcome": {
            "executed": executed,
            "refresh_performed": refreshed,
            "change_detected": detected,
        },
        "calls": {"generation": 2 if refreshed else 1, "state_snapshot": 3 if refreshed else 1},
        "latencies_ms": {"revalidation": 4.0 if refreshed else 0.0},
        "trace": [
            {"sequence": 1, "event": "generation_completed"},
            {"sequence": 2, "event": "execution", "executed": executed},
        ],
    }


def records() -> list[dict]:
    return [
        observation(
            "changed-invalid",
            "B0",
            initial_alert=True,
            final_alert=False,
            claim_value=True,
            executed=True,
            refreshed=False,
            detected=False,
        ),
        observation(
            "changed-invalid",
            "B2",
            initial_alert=True,
            final_alert=False,
            claim_value=False,
            executed=False,
            refreshed=True,
            detected=True,
        ),
        observation(
            "unchanged-valid",
            "B0",
            initial_alert=True,
            final_alert=True,
            claim_value=True,
            executed=True,
            refreshed=False,
            detected=None,
        ),
        observation(
            "unchanged-valid",
            "B2",
            initial_alert=True,
            final_alert=True,
            claim_value=True,
            executed=True,
            refreshed=False,
            detected=None,
        ),
    ]


def test_runner_computes_condition_metrics_and_paired_differences() -> None:
    result = evaluate_records(records(), PROTOCOL, bootstrap_seed=7, bootstrap_resamples=500)

    b0 = result["summary"]["conditions"]["B0"]["metrics"]
    b2 = result["summary"]["conditions"]["B2"]["metrics"]
    assert b0["response_state_consistency"]["numerator"] == 1
    assert b0["response_state_consistency"]["denominator"] == 2
    assert b0["invalid_action_execution_rate"]["value"] == 1.0
    assert b2["invalid_action_execution_rate"]["value"] == 0.0
    assert b2["valid_action_retention"]["value"] == 1.0

    comparison = result["statistics"]["comparisons"]["B2_minus_B0"]["metrics"]
    invalid_difference = comparison["invalid_action_execution"]["bootstrap"]
    assert invalid_difference["paired_count"] == 1
    assert invalid_difference["difference"] == -1.0
    assert comparison["invalid_action_execution"]["higher_is_better"] is False


def test_complete_artifact_bundle_is_hash_addressed_and_reproducible(tmp_path: Path) -> None:
    protocol_bytes = json.dumps(PROTOCOL, ensure_ascii=False, indent=2).encode("utf-8")
    common = {
        "protocol": PROTOCOL,
        "protocol_bytes": protocol_bytes,
        "repository_commit": "a" * 40,
        "run_started_at": "2026-08-07T06:00:00+00:00",
        "protocol_source": "protocols/test.json",
        "bootstrap_seed": 11,
        "bootstrap_resamples": 500,
    }

    first = run_evaluation(records(), output_dir=tmp_path / "run-a", **common)
    second = run_evaluation(records(), output_dir=tmp_path / "run-b", **common)

    expected_files = {
        "protocol.json",
        "trace.jsonl",
        "episodes.jsonl",
        "episodes.csv",
        "summary.json",
        "statistics.json",
        "metrics.csv",
        "paired-effects.csv",
        "claim-ledger.csv",
        "artifact-hashes.json",
        "run-manifest.json",
        "run-manifest.sha256",
    }
    assert {path.name for path in (tmp_path / "run-a").iterdir()} == expected_files
    assert first["run_id"] == second["run_id"]
    assert (tmp_path / "run-a" / "run-manifest.json").read_bytes() == (
        tmp_path / "run-b" / "run-manifest.json"
    ).read_bytes()

    manifest = json.loads((tmp_path / "run-a" / "run-manifest.json").read_text(encoding="utf-8"))
    assert manifest["repository_commit"] == "a" * 40
    assert manifest["protocol"]["sha256"] == sha256(protocol_bytes).hexdigest()
    assert manifest["episode_count"] == 4
    for name, metadata in manifest["artifacts"].items():
        assert sha256((tmp_path / "run-a" / name).read_bytes()).hexdigest() == metadata["sha256"]
    assert verify_artifact_bundle(tmp_path / "run-a")["status"] == "ok"
    reaggregation = verify_reaggregation(tmp_path / "run-a")
    assert reaggregation["status"] == "ok"
    assert reaggregation["summary_matches"] is True
    assert reaggregation["statistics_matches"] is True

    (tmp_path / "run-a" / "run-manifest.json").write_text("{}\n", encoding="utf-8")
    verification = verify_artifact_bundle(tmp_path / "run-a")
    assert verification["status"] == "failed"
    assert "hash_mismatch:run-manifest.json" in verification["failures"]


def test_artifact_writer_refuses_to_overwrite_an_existing_run(tmp_path: Path) -> None:
    destination = tmp_path / "existing"
    destination.mkdir()
    (destination / "keep.txt").write_text("preserve", encoding="utf-8")

    with pytest.raises(FileExistsError):
        run_evaluation(
            records(),
            protocol=PROTOCOL,
            protocol_bytes=b"{}",
            output_dir=destination,
            repository_commit="b" * 40,
            run_started_at="2026-08-07T06:00:00+00:00",
            bootstrap_resamples=10,
        )
    assert (destination / "keep.txt").read_text(encoding="utf-8") == "preserve"
