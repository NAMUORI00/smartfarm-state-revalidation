from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
import platform
from pathlib import Path
import subprocess
from typing import Sequence

from .evaluation.artifacts import canonical_json_bytes, verify_artifact_bundle
from .evaluation.runner import run_evaluation, verify_reaggregation
from .experiment import evaluation_protocol, load_json, run_suite
from .integration import check_isolated_service_contracts


def _sha256(path: Path) -> str:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return sha256(canonical_json_bytes(payload)).hexdigest()


def protocol_conditions(protocol: dict) -> tuple[str, ...]:
    """프로토콜이 선언한 조건을 선언 순서대로 돌려준다."""
    return tuple(
        str(item["condition_id"])
        for item in protocol.get("conditions", ())
        if isinstance(item, dict) and "condition_id" in item
    )


def protocol_comparisons(protocol: dict) -> tuple[tuple[str, str], ...]:
    """primary_contrasts 의 `처치-기준` 표기를 (기준, 처치) 짝으로 바꾼다."""
    pairs: list[tuple[str, str]] = []
    for item in protocol.get("primary_contrasts", ()):
        if not isinstance(item, dict):
            continue
        contrast_id = str(item.get("contrast_id", ""))
        treatment, _, baseline = contrast_id.partition("-")
        if not treatment or not baseline:
            raise ValueError(f"malformed contrast id: {contrast_id!r}")
        pairs.append((baseline, treatment))
    return tuple(pairs)


def protocol_refresh_conditions(protocol: dict) -> tuple[str, ...]:
    """갱신 예산을 가진 조건. 갱신 안전성 진단의 대상이 된다."""
    return tuple(
        str(item["condition_id"])
        for item in protocol.get("conditions", ())
        if isinstance(item, dict) and int(item.get("answer_refresh_limit", 0)) > 0
    )


def _git_output(*arguments: str) -> str:
    completed = subprocess.run(
        ("git", *arguments),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def _assert_claim_candidate_gate(
    args: argparse.Namespace,
    protocol: dict,
    scenarios: dict,
    input_hashes: dict[str, str],
    conditions: tuple[str, ...],
) -> None:
    if protocol.get("protocol_frozen") is not True:
        raise ValueError("claim_candidate requires protocol_frozen=true")
    if protocol.get("protocol_status") != "frozen" or not protocol.get("frozen_at"):
        raise ValueError("claim_candidate requires a dated frozen protocol")
    sampling = protocol.get("sampling_and_statistics", {})
    if sampling.get("repeat_count") != 1:
        raise ValueError(
            "claim_candidate requires repeat_count=1; unique scenario instances are the sample units"
        )
    if scenarios.get("suite_status") != "frozen_confirmatory":
        raise ValueError("claim_candidate requires a frozen confirmatory scenario suite")
    if protocol.get("pilot_gate", {}).get("status") != "passed":
        raise ValueError("claim_candidate requires a passed pilot gate")
    not_claimed_metrics = set(protocol.get("claim_scope", {}).get("not_claimed_metrics", ()))
    if not {"response_latency_ms", "llm_calls_tokens_and_cost"} <= not_claimed_metrics:
        raise ValueError("unmeasured latency and LLM cost must remain outside the claim scope")
    frozen_inputs = protocol.get("frozen_inputs", {})
    for name in ("scenario_sha256", "related_state_sha256"):
        if frozen_inputs.get(name) != input_hashes[name]:
            raise ValueError(f"claim_candidate input hash mismatch: {name}")
    declared = protocol_conditions(protocol)
    if conditions != declared:
        raise ValueError(
            "claim_candidate requires exactly the protocol's declared conditions: "
            + ", ".join(declared)
        )
    if _git_output("branch", "--show-current") != "main":
        raise ValueError("claim_candidate must run from main")
    if _git_output("rev-parse", "HEAD") != args.repository_commit:
        raise ValueError("repository commit does not match the checked-out HEAD")
    if _git_output("status", "--porcelain=v1", "--untracked-files=no"):
        raise ValueError("claim_candidate requires a clean tracked worktree")


def _run_experiment(args: argparse.Namespace) -> int:
    protocol = load_json(args.protocol)
    scenarios = load_json(args.scenarios)
    related_state = load_json(args.related_state)
    input_hashes = {
        "protocol_sha256": _sha256(args.protocol),
        "scenario_sha256": _sha256(args.scenarios),
        "related_state_sha256": _sha256(args.related_state),
    }
    conditions = tuple(args.conditions) or protocol_conditions(protocol)
    if args.claim_status == "claim_candidate":
        _assert_claim_candidate_gate(args, protocol, scenarios, input_hashes, conditions)
    records = run_suite(
        scenarios,
        related_state,
        conditions=conditions,
        diagnostic_conditions=protocol_refresh_conditions(protocol),
    )
    effective_protocol = evaluation_protocol(protocol, related_state)
    protocol_bundle = {
        "protocol": protocol,
        "related_state": related_state,
        "scenario_suite": scenarios,
    }
    protocol_bytes = canonical_json_bytes(protocol_bundle)
    prompt_material = {
        "generator_contract": protocol.get("generator_contract"),
        "fixed_evidence_sets": scenarios.get("fixed_evidence_sets"),
        "questions": [item.get("question") for item in scenarios.get("scenarios", ())],
    }
    prompt_sha256 = sha256(canonical_json_bytes(prompt_material)).hexdigest()
    result = run_evaluation(
        records,
        protocol=effective_protocol,
        protocol_bytes=protocol_bytes,
        output_dir=args.output,
        repository_commit=args.repository_commit,
        run_started_at=args.run_started_at,
        protocol_source=str(args.protocol),
        claim_status=args.claim_status,
        comparisons=protocol_comparisons(protocol),
        bootstrap_seed=args.bootstrap_seed,
        bootstrap_resamples=args.bootstrap_resamples,
        run_id=args.run_id,
        input_hashes=input_hashes,
        scenario_count=len(scenarios.get("scenarios", ())),
        configuration={
            "condition_ids": list(conditions),
            "generator": "deterministic_snapshot_echo",
            "executor": "dry_run",
            "human_or_llm_judge": False,
        },
        prompt_sha256=prompt_sha256,
        runtime_versions={
            "python": platform.python_version(),
            "platform": platform.platform(),
            "package": "smartfarm-state-revalidation-0.1.0",
        },
        claimed_metrics=tuple(protocol.get("claim_scope", {}).get("claimed_metrics", ())),
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def _integration_check(args: argparse.Namespace) -> int:
    result = check_isolated_service_contracts(os.environ)
    output_dir = Path(os.environ.get("THESIS_OUTPUT_DIR", args.output))
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / "integration-contract.json"
    if destination.exists():
        raise FileExistsError(f"integration result already exists: {destination}")
    destination.write_bytes(canonical_json_bytes(result))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def _verify(args: argparse.Namespace) -> int:
    result = verify_artifact_bundle(args.output)
    if result["status"] == "ok":
        result["reaggregation"] = verify_reaggregation(args.output)
        if result["reaggregation"]["status"] != "ok":
            result["status"] = "failed"
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the isolated SmartFarm execution-time state revalidation study."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser(
        "run", help="Run the protocol conditions and write a hash-addressed bundle."
    )
    run_parser.add_argument("--protocol", type=Path, default=Path("protocols/protocol.v1.json"))
    run_parser.add_argument("--scenarios", type=Path, default=Path("protocols/scenarios.v1.json"))
    run_parser.add_argument(
        "--related-state", type=Path, default=Path("protocols/related-state.v1.json")
    )
    run_parser.add_argument("--output", type=Path, required=True)
    run_parser.add_argument("--repository-commit", required=True)
    run_parser.add_argument(
        "--run-started-at", default=datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    run_parser.add_argument("--run-id")
    run_parser.add_argument(
        "--claim-status", choices=("not_claimed", "PILOT_ONLY", "claim_candidate"), default="not_claimed"
    )
    # 조건 목록은 프로토콜이 정한다. 비워 두면 선언된 조건을 그대로 쓴다.
    run_parser.add_argument(
        "--conditions", nargs="*", choices=("B0", "B1", "B2", "SR"), default=()
    )
    run_parser.add_argument("--bootstrap-seed", type=int, default=20_260_807)
    run_parser.add_argument("--bootstrap-resamples", type=int, default=10_000)
    run_parser.set_defaults(handler=_run_experiment)

    integration_parser = subparsers.add_parser(
        "integration-check", help="Check the isolated Telemetry and Farm MCP JSON contracts."
    )
    integration_parser.add_argument("--output", default="output")
    integration_parser.set_defaults(handler=_integration_check)

    verify_parser = subparsers.add_parser("verify-artifacts", help="Verify every artifact hash.")
    verify_parser.add_argument("--output", type=Path, required=True)
    verify_parser.set_defaults(handler=_verify)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.handler(args))
