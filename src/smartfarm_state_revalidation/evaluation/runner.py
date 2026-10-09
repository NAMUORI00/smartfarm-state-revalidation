"""Evaluation orchestration and CLI for frozen mapping-based observations."""

from __future__ import annotations

import argparse
from dataclasses import is_dataclass
from datetime import datetime, timezone
from enum import Enum
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .artifacts import canonical_json_bytes, sha256_bytes, write_artifact_bundle
from .metrics import aggregate_metrics
from .oracle import evaluate_episode
from .statistics import paired_binary_exact, paired_difference_bootstrap


def _enum_value(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


def _trace_dict(event: Any) -> dict[str, Any]:
    if isinstance(event, Mapping):
        return dict(event)
    if hasattr(event, "as_dict"):
        return dict(event.as_dict())
    if is_dataclass(event):
        return {
            name: _enum_value(getattr(event, name))
            for name in getattr(event, "__dataclass_fields__", {})
        }
    raise TypeError("trace events must be mappings or expose as_dict()")


def record_from_run_result(
    run_result: Any,
    *,
    episode_id: str,
    pair_id: str | None = None,
    repetition: int = 0,
    related_state_keys: Sequence[str] = (),
    invalid_action_opportunity: bool | None = None,
    valid_action_opportunity: bool | None = None,
    calls: Mapping[str, int | float] | None = None,
    latencies_ms: Mapping[str, int | float] | None = None,
) -> dict[str, Any]:
    """Duck-type the isolated engine RunResult without importing its contracts."""

    response = run_result.response
    candidate = response.action_candidate
    claims = [
        {
            "key": claim.key,
            "operator": "eq",
            "value": claim.value,
            "unit": getattr(claim, "unit", None),
            "observed_revision": getattr(claim, "observed_revision", None),
        }
        for claim in response.state_claims
    ]
    preconditions = [] if candidate is None else [
        {
            "key": item.key,
            "operator": _enum_value(item.operator),
            "expected": item.expected,
        }
        for item in candidate.preconditions
    ]
    trace = [_trace_dict(event) for event in run_result.trace]
    release_changes = any(
        event.get("barrier") == "release_check"
        and bool(event.get("details", {}).get("related_state_changed"))
        for event in trace
    )
    refresh_performed = int(run_result.refresh_count) > 0
    change_detected = refresh_performed or bool(run_result.action_blocked)
    # 답변 단위 회계. 결정론적 환경에서 잴 수 있는 구조적 생성비용이며
    # 실제 토큰 비용과는 다른 지표다.
    units = list(getattr(response, "units", ()) or ())
    revised_unit_ids = [
        unit.unit_id for unit in units if bool(getattr(unit, "revised", False))
    ]
    answer_units = {
        "declared": len(units),
        "revised": len(revised_unit_ids),
        "preserved": len(units) - len(revised_unit_ids),
        "revised_unit_ids": revised_unit_ids,
        # 최초 생성은 모든 단위를 만들고, 갱신은 다시 만든 단위만 추가한다.
        "generated_total": len(units) + (len(revised_unit_ids) if refresh_performed else 0),
    }
    inferred_calls = {
        "generation": 1 + int(run_result.refresh_count),
        "refresh_generation": int(run_result.refresh_count),
        "unit_generation": answer_units["generated_total"],
        "state_snapshot": sum(
            event.get("barrier") in {"initial_state_read", "post_generation_read", "execution_check"}
            for event in trace
        ),
    }
    if calls:
        inferred_calls.update(calls)
    return {
        "episode_id": episode_id,
        "pair_id": pair_id or episode_id,
        "condition_id": str(run_result.condition),
        "repetition": repetition,
        "related_state_keys": list(related_state_keys),
        "states": {
            "initial": dict(run_result.s0.snapshot.values),
            "response": dict(run_result.s_release.snapshot.values),
            "execution": dict(run_result.s_exec.snapshot.values),
        },
        "state_revisions": {
            "initial": int(run_result.s0.snapshot.revision),
            "response": int(run_result.s_release.snapshot.revision),
            "execution": int(run_result.s_exec.snapshot.revision),
        },
        "response": {"claims": claims},
        "answer_units": answer_units,
        "action": {
            "candidate": (
                None
                if candidate is None
                else {
                    "action": candidate.action,
                    "target": candidate.target,
                    "value": candidate.value,
                    "zone_id": candidate.zone_id,
                    "bound_state_revision": candidate.bound_state_revision,
                }
            ),
            "preconditions": preconditions,
        },
        "outcome": {
            "executed": bool(run_result.execution.applied),
            "refresh_required": release_changes,
            "refresh_performed": refresh_performed,
            "change_expected": release_changes,
            "change_detected": change_detected if release_changes else None,
            "invalid_action_opportunity": invalid_action_opportunity,
            "valid_action_opportunity": valid_action_opportunity,
            "candidate_retained": candidate is not None,
        },
        "calls": inferred_calls,
        "latencies_ms": dict(latencies_ms or {}),
        "trace": trace,
    }


# 이진 성공/실패가 아니라 개수로 재는 지표. 정확검정 대신 부트스트랩만 적용한다.
NUMERIC_PAIRED_METRICS = frozenset({"unit_generation_count"})

# 값이 작을수록 좋은 지표.
LOWER_IS_BETTER_METRICS = frozenset(
    {"invalid_action_execution", "unnecessary_refresh", "unit_generation_count"}
)


def _metric_observation(row: Mapping[str, Any], metric_name: str) -> float | bool | None:
    if metric_name == "unit_generation_count":
        units = row.get("answer_units")
        if not isinstance(units, Mapping):
            return None
        value = units.get("generated_total")
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    if metric_name == "response_state_consistency":
        value = row.get("response_claim_consistent")
        return value if isinstance(value, bool) else None
    if metric_name == "invalid_action_execution":
        if row.get("invalid_action_opportunity") is not True:
            return None
        value = row.get("executed")
        return value is True and row.get("action_valid") is False if isinstance(value, bool) else None
    if metric_name == "valid_action_retention":
        if row.get("valid_action_opportunity") is not True:
            return None
        value = row.get("candidate_retained")
        return value is True and row.get("action_valid") is True if isinstance(value, bool) else None
    if metric_name == "unnecessary_refresh":
        if row.get("refresh_required") is not False:
            return None
        value = row.get("refresh_performed")
        return value if isinstance(value, bool) else None
    if metric_name == "change_detection":
        if row.get("change_expected") is not True:
            return None
        value = row.get("change_detected")
        return value if isinstance(value, bool) else None
    raise KeyError(metric_name)


def _condition_pairs(condition_ids: Sequence[str]) -> list[tuple[str, str]]:
    ordered = sorted(set(condition_ids))
    if len(ordered) < 2:
        return []
    baseline = ordered[0]
    return [(baseline, treatment) for treatment in ordered[1:]]


def paired_statistics(
    rows: Sequence[Mapping[str, Any]],
    *,
    comparisons: Sequence[tuple[str, str]] | None = None,
    bootstrap_seed: int = 20_260_807,
    bootstrap_resamples: int = 10_000,
) -> dict[str, Any]:
    indexed: dict[tuple[str, int, str], Mapping[str, Any]] = {}
    for row in rows:
        key = (str(row.get("pair_id")), int(row.get("repetition", 0)), str(row.get("condition_id")))
        if key in indexed:
            raise ValueError(f"duplicate paired observation: {key}")
        indexed[key] = row
    comparisons = list(comparisons or _condition_pairs([str(row.get("condition_id")) for row in rows]))
    metric_names = (
        "response_state_consistency",
        "invalid_action_execution",
        "valid_action_retention",
        "unnecessary_refresh",
        "change_detection",
        "unit_generation_count",
    )
    output: dict[str, Any] = {}
    pair_keys = sorted({(key[0], key[1]) for key in indexed})
    for baseline_id, treatment_id in comparisons:
        comparison_id = f"{treatment_id}_minus_{baseline_id}"
        metric_output: dict[str, Any] = {}
        for metric_name in metric_names:
            baseline_values: list[float | bool | None] = []
            treatment_values: list[float | bool | None] = []
            for pair_key in pair_keys:
                baseline_row = indexed.get((*pair_key, baseline_id))
                treatment_row = indexed.get((*pair_key, treatment_id))
                if baseline_row is None or treatment_row is None:
                    raise ValueError(
                        f"incomplete pair {pair_key}: expected {baseline_id} and {treatment_id}"
                    )
                baseline_value = _metric_observation(baseline_row, metric_name)
                treatment_value = _metric_observation(treatment_row, metric_name)
                if (baseline_value is None) != (treatment_value is None):
                    raise ValueError(f"opportunity mismatch for {pair_key} and {metric_name}")
                baseline_values.append(baseline_value)
                treatment_values.append(treatment_value)
            bootstrap = paired_difference_bootstrap(
                baseline_values,
                treatment_values,
                seed=bootstrap_seed,
                resamples=bootstrap_resamples,
            )
            higher_is_better = metric_name not in LOWER_IS_BETTER_METRICS
            if metric_name in NUMERIC_PAIRED_METRICS:
                # 개수 지표에는 이진 정확검정을 적용할 수 없다.
                exact = {
                    "status": "not_applicable",
                    "method": "exact_mcnemar",
                    "reason": "metric_is_not_binary",
                }
            else:
                exact = paired_binary_exact(
                    baseline_values if higher_is_better else [None if value is None else not value for value in baseline_values],
                    treatment_values if higher_is_better else [None if value is None else not value for value in treatment_values],
                )
            metric_output[metric_name] = {
                "direction": "treatment_minus_baseline",
                "higher_is_better": higher_is_better,
                "bootstrap": bootstrap,
                "exact": exact,
            }
        output[comparison_id] = {
            "baseline": baseline_id,
            "treatment": treatment_id,
            "metrics": metric_output,
        }
    return {
        "schema_version": "smartfarm-state-revalidation-statistics-v1",
        "bootstrap_seed": bootstrap_seed,
        "bootstrap_resamples": bootstrap_resamples,
        "comparisons": output,
    }


def _flatten_traces(source: Mapping[str, Any], evaluated: Mapping[str, Any]) -> list[dict[str, Any]]:
    trace = source.get("trace", ())
    if not isinstance(trace, Sequence) or isinstance(trace, (str, bytes)):
        return []
    identity = {
        "episode_id": evaluated["episode_id"],
        "pair_id": evaluated["pair_id"],
        "condition_id": evaluated["condition_id"],
        "repetition": evaluated["repetition"],
    }
    return [
        {**identity, **_trace_dict(event)}
        for event in trace
    ]


def evaluate_records(
    records: Iterable[Mapping[str, Any]],
    protocol: Mapping[str, Any] | None = None,
    *,
    comparisons: Sequence[tuple[str, str]] | None = None,
    bootstrap_seed: int = 20_260_807,
    bootstrap_resamples: int = 10_000,
) -> dict[str, Any]:
    source_rows = [dict(record) for record in records]
    evaluated = [evaluate_episode(record, protocol) for record in source_rows]
    aggregated = summarize_evaluated_records(
        evaluated,
        comparisons=comparisons,
        bootstrap_seed=bootstrap_seed,
        bootstrap_resamples=bootstrap_resamples,
    )
    traces = [
        event
        for source, row in zip(source_rows, evaluated, strict=True)
        for event in _flatten_traces(source, row)
    ]
    return {
        "episodes": evaluated,
        "traces": traces,
        **aggregated,
    }


def summarize_evaluated_records(
    evaluated: Sequence[Mapping[str, Any]],
    *,
    comparisons: Sequence[tuple[str, str]] | None = None,
    bootstrap_seed: int = 20_260_807,
    bootstrap_resamples: int = 10_000,
) -> dict[str, Any]:
    primary = [row for row in evaluated if row.get("comparison_scope") == "paired_primary"]
    diagnostics = [row for row in evaluated if row.get("comparison_scope") != "paired_primary"]
    conditions = sorted({str(row["condition_id"]) for row in primary})
    by_condition = {
        condition: aggregate_metrics(row for row in primary if row["condition_id"] == condition)
        for condition in conditions
    }
    summary = {
        "schema_version": "smartfarm-state-revalidation-summary-v1",
        "episode_result_count": len(evaluated),
        "primary_episode_result_count": len(primary),
        "diagnostic_episode_result_count": len(diagnostics),
        "condition_count": len(conditions),
        "conditions": by_condition,
        "overall": aggregate_metrics(primary),
        "diagnostics": aggregate_metrics(diagnostics),
    }
    statistics = paired_statistics(
        primary,
        comparisons=comparisons,
        bootstrap_seed=bootstrap_seed,
        bootstrap_resamples=bootstrap_resamples,
    )
    return {
        "summary": summary,
        "statistics": statistics,
    }


def verify_reaggregation(output_dir: str | Path) -> dict[str, Any]:
    destination = Path(output_dir)
    manifest = json.loads((destination / "run-manifest.json").read_text(encoding="utf-8"))
    episodes = [
        json.loads(line)
        for line in (destination / "episodes.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    configuration = manifest.get("configuration", {})
    comparisons = configuration.get("comparisons")
    normalized_comparisons = (
        tuple((str(item[0]), str(item[1])) for item in comparisons)
        if isinstance(comparisons, list)
        else None
    )
    aggregated = summarize_evaluated_records(
        episodes,
        comparisons=normalized_comparisons,
        bootstrap_seed=int(manifest.get("random_seeds", {}).get("paired_bootstrap", 20_260_807)),
        bootstrap_resamples=int(configuration.get("bootstrap_resamples", 10_000)),
    )
    summary_matches = canonical_json_bytes(aggregated["summary"]) == (
        destination / "summary.json"
    ).read_bytes()
    statistics_matches = canonical_json_bytes(aggregated["statistics"]) == (
        destination / "statistics.json"
    ).read_bytes()
    return {
        "status": "ok" if summary_matches and statistics_matches else "failed",
        "summary_matches": summary_matches,
        "statistics_matches": statistics_matches,
        "episode_count": len(episodes),
    }


def run_evaluation(
    records: Iterable[Mapping[str, Any]],
    *,
    protocol: Mapping[str, Any],
    protocol_bytes: bytes,
    output_dir: str | Path,
    repository_commit: str,
    run_started_at: str,
    protocol_source: str | None = None,
    claim_status: str = "not_claimed",
    comparisons: Sequence[tuple[str, str]] | None = None,
    bootstrap_seed: int = 20_260_807,
    bootstrap_resamples: int = 10_000,
    run_id: str | None = None,
    input_hashes: Mapping[str, str] | None = None,
    scenario_count: int | None = None,
    configuration: Mapping[str, Any] | None = None,
    prompt_sha256: str | None = None,
    runtime_versions: Mapping[str, str] | None = None,
    claimed_metrics: Sequence[str] = (),
) -> dict[str, Any]:
    if not repository_commit.strip():
        raise ValueError("repository_commit is required")
    if not run_started_at.strip():
        raise ValueError("run_started_at is required")
    source_rows = [dict(record) for record in records]
    evaluated = evaluate_records(
        source_rows,
        protocol,
        comparisons=comparisons,
        bootstrap_seed=bootstrap_seed,
        bootstrap_resamples=bootstrap_resamples,
    )
    if claim_status == "claim_candidate":
        protocol_errors = sum(int(row.get("protocol_error_count", 0)) for row in evaluated["episodes"])
        if protocol_errors:
            raise ValueError(f"claim_candidate has {protocol_errors} protocol errors")
        if any(row.get("oracle_state_match") is not True for row in evaluated["episodes"]):
            raise ValueError("claim_candidate contains an oracle state mismatch")
    input_hash = sha256_bytes(canonical_json_bytes(source_rows))
    metadata = {
        "run_id": run_id,
        "claim_status": claim_status,
        "repository_commit": repository_commit,
        "run_started_at": run_started_at,
        "protocol_source": protocol_source,
        "observation_input_sha256": input_hash,
        "prompt_sha256": prompt_sha256,
        "input_hashes": dict(input_hashes or {}),
        "scenario_count": scenario_count,
        "random_seeds": {"paired_bootstrap": bootstrap_seed},
        "runtime_versions": dict(runtime_versions or {}),
        "configuration": {
            "bootstrap_resamples": bootstrap_resamples,
            "comparisons": [list(item) for item in comparisons] if comparisons else None,
            **dict(configuration or {}),
        },
        "claimed_metrics": list(claimed_metrics),
    }
    return write_artifact_bundle(
        output_dir,
        protocol_bytes=protocol_bytes,
        traces=evaluated["traces"],
        episodes=evaluated["episodes"],
        summary=evaluated["summary"],
        statistics=evaluated["statistics"],
        manifest_metadata=metadata,
    )


def _load_records(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() == ".jsonl":
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, Mapping):
        payload = payload.get("episodes", payload.get("records"))
    if not isinstance(payload, list) or not all(isinstance(item, Mapping) for item in payload):
        raise ValueError("observation input must be a JSON list or JSONL mappings")
    return [dict(item) for item in payload]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate frozen SmartFarm state-revalidation observations.")
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--observations", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repository-commit", required=True)
    parser.add_argument("--run-started-at", default=datetime.now(timezone.utc).isoformat())
    parser.add_argument("--claim-status", default="not_claimed")
    parser.add_argument("--bootstrap-seed", type=int, default=20_260_807)
    parser.add_argument("--bootstrap-resamples", type=int, default=10_000)
    args = parser.parse_args(argv)

    protocol_bytes = args.protocol.read_bytes()
    protocol = json.loads(protocol_bytes)
    result = run_evaluation(
        _load_records(args.observations),
        protocol=protocol,
        protocol_bytes=protocol_bytes,
        output_dir=args.output,
        repository_commit=args.repository_commit,
        run_started_at=args.run_started_at,
        protocol_source=str(args.protocol),
        claim_status=args.claim_status,
        bootstrap_seed=args.bootstrap_seed,
        bootstrap_resamples=args.bootstrap_resamples,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
