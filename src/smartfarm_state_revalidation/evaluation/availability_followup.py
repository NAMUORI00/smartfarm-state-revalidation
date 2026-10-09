"""Execute the original meeting's availability tests against unchanged SR code.

The fixture reader and recorders only inject/observe; they never recover errors,
rewrite answers, or fabricate a device hold. The oracle sees serialized records.
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import fields, is_dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from hashlib import sha256
from html import escape
import json
from pathlib import Path
import platform
import subprocess
from collections.abc import Mapping

from ..contracts import StateSnapshot
from ..engine import ExperimentEngine
from ..executor import DryRunExecutor
from ..experiment import action_factory, related_state_spec
from ..generator import SnapshotEchoGenerator
from ..policies import SR_SELECTIVE_REVISION
from ..trace import TraceRecorder

ROOT = "sensor.zone-a.root_zone_temperature_c"
FIXED_TIME = datetime(2026, 10, 4, tzinfo=timezone.utc)
RESEARCH = Path(__file__).resolve().parents[3]


def plain(value):
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value):
        return {f.name: plain(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


def canonical(value):
    return json.dumps(plain(value), ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


class TemperatureReadError(OSError):
    pass


class WholeSnapshotReadError(OSError):
    pass


class FixtureReader:
    def __init__(self, condition, variant):
        self.condition = condition
        self.reads = []
        self.initial = StateSnapshot(
            revision=100,
            observed_at=FIXED_TIME,
            values={
                ROOT: 19.0,
                "sensor.zone-a.relative_humidity_pct": 85.0,
                "alert.zone-a.root_temperature_high.active": True,
                "alert.zone-a.root_temperature_high.occurrence_id": "root-event",
                "alert.zone-a.humidity_high.active": True,
                "alert.zone-a.humidity_high.occurrence_id": "humidity-event",
                "alert.zone-a.air_temperature_sensor_health.active": False,
                "device.zone-a.root_zone_chiller.enabled": False,
                "device.zone-a.circulation_fan.percent": variant["initial_fan_percent"],
                "telemetry.farm.available": True,
                "telemetry.farm.sample_age_seconds": variant["sample_age_seconds"],
            },
        )

    def read(self):
        index = len(self.reads) + 1
        record = {"read_number": index, "fault_source": None}
        self.reads.append(record)
        if index >= 2 and self.condition in {"temperature_read_error", "whole_snapshot_error"}:
            partial = dict(self.initial.values)
            partial.pop(ROOT)
            record.update({
                "fault_source": ROOT if self.condition == "temperature_read_error" else "all_state",
                "returned_snapshot": None,
                "independent_source_values_available": partial if self.condition == "temperature_read_error" else {},
            })
            error = TemperatureReadError if self.condition == "temperature_read_error" else WholeSnapshotReadError
            raise error("injected at release re-read")
        values = dict(self.initial.values)
        if self.condition == "persistent_missing":
            values.pop(ROOT)
        snapshot = StateSnapshot(99 + index, values, FIXED_TIME + timedelta(seconds=index - 1))
        record["returned_snapshot"] = plain(snapshot)
        return snapshot


class RecordingGenerator(SnapshotEchoGenerator):
    def __init__(self, factory):
        super().__init__(factory)
        self.outputs = []

    def generate(self, request, reach_barrier):
        result = super().generate(request, reach_barrier)
        self.outputs.append({"input_observed_at": request.snapshot.observed_at.isoformat(), "response": plain(result)})
        return result


class RecordingExecutor(DryRunExecutor):
    def __init__(self):
        self.calls = []

    def execute(self, candidate, state, *, enforce_preconditions):
        result = super().execute(candidate, state, enforce_preconditions=enforce_preconditions)
        self.calls.append({"candidate": plain(candidate), "state": plain(state), "result": plain(result)})
        return result


def run_probe(protocol, condition, variant, target, registry):
    cooling = target == "cooling"
    candidate = {
        "action": "set_cooling_enabled" if cooling else "set_circulation_fan_percent",
        "target": "root_zone_chiller" if cooling else "circulation_fan",
        "value": True if cooling else 25.0,
    }
    profile = "root-zone-cooling-control-v1" if cooling else "humidity-fan-control-v1"
    reader = FixtureReader(condition, variant)
    generator = RecordingGenerator(action_factory({"zone_id": "zone-a", "action_contract": {"initial_candidate": candidate}}))
    executor = RecordingExecutor()
    trace = TraceRecorder()
    record = {"target": target, "completed": False, "exception": None, "released_response": None}
    try:
        result = ExperimentEngine(reader, generator, executor, trace_sink=trace).run(
            SR_SELECTIVE_REVISION,
            question=protocol["question"],
            evidence=tuple(protocol["evidence"]),
            related_state=related_state_spec(profile, registry),
        )
        record.update({"completed": True, "released_response": plain(result.response),
                       "release_changed": result.release_diff.changed,
                       "refresh_count": result.refresh_count, "execution": plain(result.execution)})
    except Exception as error:
        # Preserve failure as an observation. Never turn it into a system hold.
        record["exception"] = {"type": type(error).__name__, "message": str(error)}
    record.update({"reads": reader.reads, "generations": generator.outputs,
                   "executor_calls": executor.calls, "trace": [e.as_dict() for e in trace.events]})
    return record


def judge(case, condition, oracle):
    """Independent assertions over logged fields, without calling core decisions."""
    cooling, fan = case["probes"]["cooling"], case["probes"]["fan"]
    response = cooling.get("released_response") or {}
    claims = [c for c in response.get("state_claims", []) if c["key"] == ROOT]
    if condition["expected_temperature"] is not None:
        answer_ok = (cooling["completed"] and len(claims) == 1
                     and claims[0]["value"] == condition["expected_temperature"]
                     and claims[0]["unit"] == "degC"
                     and claims[0]["observed_revision"] == response.get("source_revision")
                     and bool(cooling["generations"][0]["input_observed_at"]))
    else:
        # This deterministic generator has no historical-value schema. Do not
        # reinterpret a stale numeric claim as history without such evidence.
        answer_ok = (cooling["completed"] and not any(isinstance(c["value"], (int, float)) for c in claims)
                     and any(m in response.get("text", "") for m in oracle["unavailable_text_markers"]))

    def action_ok(probe, expectation, expected_value, expected_action):
        if not probe["completed"] or not probe["executor_calls"]:
            return False
        call = probe["executor_calls"][-1]
        result, candidate = call["result"], call["candidate"]
        if expectation in {"allow", "retain"}:
            values = call["state"]["values"]
            fresh = (values.get("telemetry.farm.available") is True
                     and isinstance(values.get("telemetry.farm.sample_age_seconds"), (int, float))
                     and 0 <= values["telemetry.farm.sample_age_seconds"] <= 120)
            if expected_action == "set_cooling_enabled":
                temperature = values.get(ROOT)
                independently_valid = (fresh and isinstance(temperature, (int, float))
                    and not isinstance(temperature, bool) and temperature > 18
                    and values.get("alert.zone-a.root_temperature_high.active") is True
                    and values.get("device.zone-a.root_zone_chiller.enabled") is False)
            else:
                humidity = values.get("sensor.zone-a.relative_humidity_pct")
                output = values.get("device.zone-a.circulation_fan.percent")
                independently_valid = (fresh and isinstance(humidity, (int, float)) and humidity > 80
                    and values.get("alert.zone-a.humidity_high.active") is True
                    and values.get("alert.zone-a.air_temperature_sensor_health.active") is False
                    and isinstance(output, (int, float)) and output < 25)
            return bool(candidate and candidate["value"] == expected_value
                        and candidate["action"] == expected_action
                        and independently_valid
                        and result["applied"] and result["valid"] is True
                        and not result["failed_preconditions"])
        return (not result["applied"] and result.get("failure_code") in oracle["hold_reason_codes"])

    scores = {
        "A": int(bool(answer_ok)),
        "C": int(action_ok(cooling, condition["cooling"], True, "set_cooling_enabled")),
        "F": int(action_ok(fan, condition["fan"], 25.0, "set_circulation_fan_percent")),
    }
    scores["S"] = scores["A"] * scores["C"] * scores["F"]
    if any(p["exception"] for p in case["probes"].values()):
        scores["S"] = 0
    return scores


def execute(protocol, registry):
    cases = []
    for condition in protocol["conditions"]:
        for variant in protocol["variants"]:
            case = {"case_id": condition["id"] + "-" + variant["id"], "condition": condition["id"],
                    "variant": variant, "primary": condition["primary"],
                    "probes": {target: run_probe(protocol, condition["id"], variant, target, registry)
                               for target in ("cooling", "fan")}}
            case["scores"] = judge(case, condition, protocol["oracle"])
            cases.append(case)
    return cases


def summarize(cases, protocol):
    result = []
    for condition in protocol["conditions"]:
        rows = [c for c in cases if c["condition"] == condition["id"]]
        result.append({"condition": condition["id"], "label": condition["label"], "primary": condition["primary"],
                       "N": len(rows), "engine_runs": 2 * len(rows),
                       "exceptions": sum(any(p["exception"] for p in c["probes"].values()) for c in rows),
                       "metrics": {k: {"passed": sum(c["scores"][k] for c in rows),
                                       "percent": 100 * sum(c["scores"][k] for c in rows) / len(rows)}
                                   for k in ("A", "C", "F", "S")}})
    return result


def make_report(summary, cases):
    table = "".join("<tr><th>" + escape(row["label"] + ("" if row["primary"] else " (별도 진단)")) + "</th>"
                    + "".join(f'<td>{row["metrics"][k]["percent"]:.0f}% <small>({row["metrics"][k]["passed"]}/{row["N"]})</small></td>' for k in ("A", "C", "F", "S"))
                    + f'<td>{row["exceptions"]}/{row["N"]}</td></tr>' for row in summary)
    examples = ""
    for c in cases:
        if c["variant"]["id"] != "v01":
            continue
        p = c["probes"]["cooling"]
        excerpt = {"condition": c["condition"], "initial_answer": p["generations"][0]["response"]["units"][0],
                   "released_answer": p["released_response"], "exception": p["exception"],
                   "cooling_execution": p["executor_calls"], "scores": c["scores"]}
        examples += f'<details><summary>{escape(c["condition"])} · 실제 기록</summary><pre>{escape(canonical(excerpt))}</pre></details>'
    return '''<!doctype html><html lang="ko"><meta charset="utf-8"><title>센서 값 가용성 추가 검증 결과</title>
<style>body{font:17px/1.6 "Malgun Gothic",sans-serif;color:#203635;background:#f6f7f7;margin:40px auto;max-width:1150px;padding:30px}h1{font-size:32px}h2{font-size:23px;margin-top:36px}table{border-collapse:collapse;width:100%;background:white}td,th{padding:16px;border-bottom:1px solid #d3dfdf;text-align:left}thead{background:#e0eded}small{display:block;color:#627575}pre{white-space:pre-wrap;font-size:13px;background:white;padding:20px}summary{cursor:pointer;padding:12px}strong{color:#176b64}.flow{padding:22px;background:#e0eded}li{margin:8px 0}</style>
<h1>센서 값을 받지 못할 때의 추가 검증</h1>
<p>원본 미팅 15–17장에 예고한 정상·예외 조건을 <strong>기존 선택적 수정 연구 코어</strong>에 적용한 결과입니다.</p>
<p class="flow">고정 입력 10종 × 조회 조건 → 냉각·팬 각각 실행 → 답변과 실행 기록 대조 → 조건별 통과 수 / 전체 수</p>
<h2>조건별 판정 결과</h2><table><thead><tr><th>조회 조건</th><th>답변 적합 A</th><th>냉각 처리 C</th><th>팬 처리 F</th><th>모두 충족 S</th><th>오류 종료</th></tr></thead><tbody>''' + table + '''</tbody></table>
<p>A: 정상 시 19°C 표시, 예외 시 현재 온도 확인 불가 안내. C: 정상 시 냉각 허용, 예외 시 이유를 남기고 실행하지 않음. F: 독립 조건이 유효한 팬 25% 조정안 유지. S=A×C×F.</p>
<h2>실제 동작의 차이</h2><ul>
<li><strong>정상 조회:</strong> 19°C가 답변에 남고, 냉각과 팬의 실행 조건 검사를 통과합니다.</li>
<li><strong>처음부터 값 없음:</strong> 온도는 None으로 기록되며 확인 불가 안내가 없습니다. 냉각 후보는 없지만, 왜 보류했는지 구분하는 이유가 없습니다. 팬은 별도 실행에서 유지됩니다.</li>
<li><strong>온도 재조회 오류:</strong> 오류가 상태 스냅샷 조회 밖으로 전파되어 두 실행이 중단됩니다. 초기 19°C 답변은 내부 생성 기록이며 최종 공개된 답변이 아닙니다.</li>
</ul><h2>해석 범위</h2><p>현재 구현의 빈틈을 찾는 진단 실험입니다. 개선 후 성능을 측정한 것이 아닙니다. 조건당 10개는 팬 초기 출력 0–9%, 관측 나이 10–19초를 조합한 결정론적 입력이며 독립적인 농장 표본이나 LLM 응답 10회가 아닙니다.</p>
<p>기존 엔진은 실행당 조정안 하나만 지원하므로, 동일 입력의 냉각·팬 실행 두 건을 묶었습니다. 동시 제어를 검증하지 않았습니다. 오류 입력은 온도 원천에 한정했으나 현재 조회 인터페이스가 전체 스냅샷을 반환하므로 한 값의 조회 예외가 전체 호출을 중단합니다. 전체 조회 실패는 별도 진단으로 집계했습니다.</p>
<p>주 평가 30개 상황·60회 엔진 실행, 별도 진단 10개 상황·20회 엔진 실행. 재현 확인을 위한 재실행은 분모에 더하지 않습니다. 실제 LLM·API 배포판·MCP·실물 장치는 호출하지 않았습니다.</p>
<h2>원본 실행 기록</h2>''' + examples + '</html>'


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    args.output.mkdir(parents=True, exist_ok=False)
    protocol_path = RESEARCH / "protocols/availability-followup.v1.json"
    registry_path = RESEARCH / "protocols/related-state.v2.json"
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    protected = sorted((RESEARCH / "src").rglob("*.py")) + sorted((RESEARCH / "protocols").glob("*.json")) + sorted((RESEARCH / "results").glob("confirmatory-*.json"))
    hashes = {str(p.relative_to(RESEARCH)).replace("\\", "/"): digest(p) for p in protected}
    # Written before the first measured engine invocation.
    manifest = {"started_at": datetime.now(timezone.utc).isoformat(), "python": platform.python_version(),
                "repository_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=RESEARCH, text=True).strip(),
                "protocol": protocol, "source_sha256": hashes, "replay_checks": 3}
    (args.output / "manifest.json").write_text(canonical(manifest), encoding="utf-8")
    cases = execute(protocol, registry)
    replay_equal = [execute(protocol, registry) == cases for _ in range(3)]
    if not all(replay_equal):
        raise RuntimeError("deterministic replay mismatch")
    if any(digest(RESEARCH / name) != expected for name, expected in hashes.items()):
        raise RuntimeError("source or frozen artifact changed during experiment")
    summary = summarize(cases, protocol)
    (args.output / "cases.json").write_text(canonical(cases), encoding="utf-8")
    (args.output / "summary.json").write_text(canonical({"conditions": summary, "replay_equal": replay_equal,
        "source_and_frozen_artifacts_unchanged": True, "completed_at": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")
    with (args.output / "scores.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["case_id", "condition", "primary", "A", "C", "F", "S"])
        writer.writeheader()
        writer.writerows({"case_id": c["case_id"], "condition": c["condition"], "primary": c["primary"], **c["scores"]} for c in cases)
    (args.output / "report.html").write_text(make_report(summary, cases), encoding="utf-8")
    (args.output / "sha256.json").write_text(canonical({p.name: digest(p) for p in sorted(args.output.iterdir()) if p.is_file()}), encoding="utf-8")
    print(canonical({"output": str(args.output.resolve()), "conditions": summary, "replay_equal": replay_equal}))


if __name__ == "__main__":
    main()
