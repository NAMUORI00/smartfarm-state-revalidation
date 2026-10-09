"""Write a complete, hash-addressed evaluation artifact bundle."""

from __future__ import annotations

from csv import DictWriter
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


def canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    return sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_bytes(path: Path, payload: bytes) -> None:
    path.write_bytes(payload)


def _jsonl_bytes(rows: Iterable[Mapping[str, Any]]) -> bytes:
    return b"".join(canonical_json_bytes(dict(row)) for row in rows)


def _csv_value(value: Any) -> Any:
    if isinstance(value, (Mapping, list, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def _csv_bytes(rows: Sequence[Mapping[str, Any]]) -> bytes:
    identity = ["episode_id", "pair_id", "condition_id", "repetition"]
    keys = {str(key) for row in rows for key in row}
    fieldnames = [name for name in identity if name in keys] + sorted(keys - set(identity))
    output = StringIO(newline="")
    if fieldnames:
        writer = DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({name: _csv_value(row.get(name)) for name in fieldnames})
    return output.getvalue().encode("utf-8")


def _metric_rows(summary: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    conditions = summary.get("conditions", {})
    if not isinstance(conditions, Mapping):
        return rows
    for condition_id, condition in sorted(conditions.items()):
        metrics = condition.get("metrics", {}) if isinstance(condition, Mapping) else {}
        if not isinstance(metrics, Mapping):
            continue
        for metric_id, metric in sorted(metrics.items()):
            if isinstance(metric, Mapping):
                rows.append({"condition_id": condition_id, "metric_id": metric_id, **metric})
    diagnostics = summary.get("diagnostics", {})
    diagnostic_metrics = diagnostics.get("metrics", {}) if isinstance(diagnostics, Mapping) else {}
    if isinstance(diagnostic_metrics, Mapping):
        for metric_id, metric in sorted(diagnostic_metrics.items()):
            if isinstance(metric, Mapping):
                rows.append({"condition_id": "B2_diagnostic", "metric_id": metric_id, **metric})
    return rows


def _paired_effect_rows(statistics: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    comparisons = statistics.get("comparisons", {})
    if not isinstance(comparisons, Mapping):
        return rows
    for comparison_id, comparison in sorted(comparisons.items()):
        if not isinstance(comparison, Mapping):
            continue
        metrics = comparison.get("metrics", {})
        if not isinstance(metrics, Mapping):
            continue
        for metric_id, metric in sorted(metrics.items()):
            if not isinstance(metric, Mapping):
                continue
            bootstrap = metric.get("bootstrap", {})
            exact = metric.get("exact", {})
            rows.append(
                {
                    "comparison_id": comparison_id,
                    "baseline": comparison.get("baseline"),
                    "treatment": comparison.get("treatment"),
                    "metric_id": metric_id,
                    "higher_is_better": metric.get("higher_is_better"),
                    "paired_count": bootstrap.get("paired_count") if isinstance(bootstrap, Mapping) else None,
                    "difference": bootstrap.get("difference") if isinstance(bootstrap, Mapping) else None,
                    "lower": bootstrap.get("lower") if isinstance(bootstrap, Mapping) else None,
                    "upper": bootstrap.get("upper") if isinstance(bootstrap, Mapping) else None,
                    "p_value_two_sided": exact.get("p_value_two_sided") if isinstance(exact, Mapping) else None,
                }
            )
    return rows


def _claim_ledger_rows(
    metrics: Sequence[Mapping[str, Any]],
    *,
    claim_status: str,
    summary_sha256: str,
    claimed_metrics: set[str],
) -> list[dict[str, Any]]:
    return [
        {
            "claim_id": f"{row.get('condition_id')}:{row.get('metric_id')}",
            "claim_status": claim_status,
            "condition_id": row.get("condition_id"),
            "metric_id": row.get("metric_id"),
            "numerator": row.get("numerator"),
            "denominator": row.get("denominator"),
            "value": row.get("value"),
            "source_artifact": "summary.json",
            "source_sha256": summary_sha256,
        }
        for row in metrics
        if row.get("metric_id") in claimed_metrics
        and row.get("status") == "ok"
        and isinstance(row.get("denominator"), int)
        and row["denominator"] > 0
    ]


def write_artifact_bundle(
    output_dir: str | Path,
    *,
    protocol_bytes: bytes,
    traces: Sequence[Mapping[str, Any]],
    episodes: Sequence[Mapping[str, Any]],
    summary: Mapping[str, Any],
    statistics: Mapping[str, Any],
    manifest_metadata: Mapping[str, Any],
) -> dict[str, Any]:
    """Create the complete bundle without overwriting an existing run."""

    destination = Path(output_dir)
    if destination.exists() and any(destination.iterdir()):
        raise FileExistsError(f"evaluation output directory is not empty: {destination}")
    destination.mkdir(parents=True, exist_ok=True)

    ordered_episodes = sorted(
        (dict(item) for item in episodes),
        key=lambda item: (
            str(item.get("pair_id", item.get("episode_id", ""))),
            str(item.get("condition_id", "")),
            int(item.get("repetition", 0)),
        ),
    )
    ordered_traces = sorted(
        (dict(item) for item in traces),
        key=lambda item: (
            str(item.get("pair_id", item.get("episode_id", ""))),
            str(item.get("condition_id", "")),
            int(item.get("repetition", 0)),
            int(item.get("sequence", 0)),
        ),
    )

    summary_payload = canonical_json_bytes(dict(summary))
    statistics_payload = canonical_json_bytes(dict(statistics))
    metric_rows = _metric_rows(summary)
    paired_rows = _paired_effect_rows(statistics)
    claim_rows = _claim_ledger_rows(
        metric_rows,
        claim_status=str(manifest_metadata.get("claim_status", "not_claimed")),
        summary_sha256=sha256_bytes(summary_payload),
        claimed_metrics=set(manifest_metadata.get("claimed_metrics", ())),
    )
    payloads = {
        "protocol.json": protocol_bytes,
        "trace.jsonl": _jsonl_bytes(ordered_traces),
        "episodes.jsonl": _jsonl_bytes(ordered_episodes),
        "episodes.csv": _csv_bytes(ordered_episodes),
        "summary.json": summary_payload,
        "statistics.json": statistics_payload,
        "metrics.csv": _csv_bytes(metric_rows),
        "paired-effects.csv": _csv_bytes(paired_rows),
        "claim-ledger.csv": _csv_bytes(claim_rows),
    }
    for name, payload in payloads.items():
        _write_bytes(destination / name, payload)

    artifact_hashes = {
        name: {
            "sha256": sha256_bytes(payload),
            "bytes": len(payload),
        }
        for name, payload in sorted(payloads.items())
    }
    hashes_payload = canonical_json_bytes(
        {
            "schema_version": "smartfarm-state-revalidation-artifact-hashes-v1",
            "artifacts": artifact_hashes,
        }
    )
    _write_bytes(destination / "artifact-hashes.json", hashes_payload)
    artifact_hashes["artifact-hashes.json"] = {
        "sha256": sha256_bytes(hashes_payload),
        "bytes": len(hashes_payload),
    }

    stable_identity = {
        "protocol_sha256": sha256_bytes(protocol_bytes),
        "repository_commit": manifest_metadata.get("repository_commit"),
        "observation_input_sha256": manifest_metadata.get("observation_input_sha256"),
        "prompt_sha256": manifest_metadata.get("prompt_sha256"),
        "input_hashes": dict(manifest_metadata.get("input_hashes", {})),
        "artifacts": artifact_hashes,
    }
    run_id = str(manifest_metadata.get("run_id") or f"state-revalidation-{sha256_bytes(canonical_json_bytes(stable_identity))[:16]}")
    manifest = {
        "schema_version": "smartfarm-state-revalidation-run-manifest-v1",
        "run_id": run_id,
        "claim_status": str(manifest_metadata.get("claim_status", "not_claimed")),
        "repository_commit": manifest_metadata.get("repository_commit"),
        "run_started_at": manifest_metadata.get("run_started_at"),
        "protocol": {
            "path": "protocol.json",
            "sha256": sha256_bytes(protocol_bytes),
            "source": manifest_metadata.get("protocol_source"),
        },
        "observation_input_sha256": manifest_metadata.get("observation_input_sha256"),
        "prompt_sha256": manifest_metadata.get("prompt_sha256"),
        "input_hashes": dict(manifest_metadata.get("input_hashes", {})),
        "episode_count": len(ordered_episodes),
        "scenario_count": manifest_metadata.get("scenario_count"),
        "trace_event_count": len(ordered_traces),
        "random_seeds": dict(manifest_metadata.get("random_seeds", {})),
        "runtime_versions": dict(manifest_metadata.get("runtime_versions", {})),
        "configuration": dict(manifest_metadata.get("configuration", {})),
        "artifacts": artifact_hashes,
    }
    manifest_payload = canonical_json_bytes(manifest)
    _write_bytes(destination / "run-manifest.json", manifest_payload)
    manifest_sha256 = sha256_bytes(manifest_payload)
    _write_bytes(
        destination / "run-manifest.sha256",
        f"{manifest_sha256}  run-manifest.json\n".encode("ascii"),
    )
    return {
        "output_dir": str(destination.resolve()),
        "run_id": run_id,
        "run_manifest": manifest,
        "run_manifest_sha256": manifest_sha256,
        "artifact_hashes": artifact_hashes,
    }


def verify_artifact_bundle(output_dir: str | Path) -> dict[str, Any]:
    destination = Path(output_dir)
    manifest_path = destination / "run-manifest.json"
    sidecar_path = destination / "run-manifest.sha256"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    failures: list[str] = []
    if not sidecar_path.is_file():
        failures.append("missing:run-manifest.sha256")
    else:
        expected_manifest_hash = sidecar_path.read_text(encoding="ascii").split()[0]
        if sha256_file(manifest_path) != expected_manifest_hash:
            failures.append("hash_mismatch:run-manifest.json")
    for name, metadata in manifest.get("artifacts", {}).items():
        path = destination / name
        if not path.is_file():
            failures.append(f"missing:{name}")
            continue
        if sha256_file(path) != metadata.get("sha256"):
            failures.append(f"hash_mismatch:{name}")
        if path.stat().st_size != metadata.get("bytes"):
            failures.append(f"size_mismatch:{name}")
    return {
        "status": "ok" if not failures else "failed",
        "run_id": manifest.get("run_id"),
        "checked_artifact_count": len(manifest.get("artifacts", {})),
        "failures": failures,
    }
