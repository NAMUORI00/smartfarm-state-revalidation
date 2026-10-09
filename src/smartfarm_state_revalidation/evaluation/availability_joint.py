"""Predeclared joint availability cases, policy comparison and raw evidence."""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import platform
import subprocess
import csv
import html
from zipfile import ZipFile, ZIP_DEFLATED

from ..availability import (
    ROOT, HUMIDITY, COOLER, FAN, PERMIT_COOLER, PERMIT_FAN, AVAILABLE,
    ROOT_ALERT, ROOT_EVENT, HUMIDITY_ALERT, HUMIDITY_EVENT, HEALTH,
    FieldReadError, SnapshotUnavailable, collect, run_joint, _snapshot, _legacy_read_guard,
)
from ..engine import ExperimentEngine
from ..executor import DryRunExecutor
from ..experiment import action_factory, related_state_spec
from ..policies import SR_SELECTIVE_REVISION
from .availability_followup import RESEARCH, plain, RecordingGenerator

T0 = datetime(2026, 10, 4, tzinfo=timezone.utc)
BASE = {ROOT:19.0, HUMIDITY:85.0, COOLER:False, FAN:0.0, PERMIT_COOLER:True,
        PERMIT_FAN:True, AVAILABLE:True, ROOT_ALERT:True, ROOT_EVENT:"root-event",
        HUMIDITY_ALERT:True, HUMIDITY_EVENT:"humidity-event", HEALTH:False}


def dump(value):
    return json.dumps(plain(value), ensure_ascii=False, sort_keys=True, indent=2) + "\n"


class ScriptedSource:
    """One real field source boundary: return healthy fields despite another failure."""
    def __init__(self, case):
        self.case = case
        self.reads = []

    def begin(self, stage, at):
        self.stage, self.at = stage, at
        self.setting = self.case["stages"].get(stage, {})
        self.reads.append({"stage":stage, "event":"begin", "at":at.isoformat()})
        if self.setting.get("snapshot_error"):
            self.reads[-1]["outcome"] = "snapshot_unavailable"
            raise SnapshotUnavailable("fixture whole-source outage")

    def read(self, key):
        event = {"stage":self.stage, "key":key}
        self.reads.append(event)
        if key in self.setting.get("errors", []):
            event["outcome"] = "read_error"
            raise FieldReadError("fixture field-source failure")
        if key in self.setting.get("missing", []):
            event["outcome"] = "missing"
            raise KeyError(key)
        value = self.setting.get("values", {}).get(key, BASE[key])
        at = self.at - timedelta(seconds=self.setting.get("ages", {}).get(key, 10.0))
        event.update({"outcome":"returned", "value":value, "observed_at":at.isoformat()})
        return value, at


def times(protocol):
    return {k:T0+timedelta(seconds=v) for k,v in protocol["stage_times_seconds"].items()}


def score(record, case, protocol):
    """Judge output against manual expectations and input evidence, not engine verdicts."""
    expected = case["expected"]
    answer = (record.get("released_answer") or {}).get(ROOT)
    A = False
    if answer and answer["status"] == expected["answer"]["status"] and answer["value"] == expected["answer"]["value"]:
        if answer["status"] == "current":
            allowed_times = {
                (times(protocol)[s] - timedelta(seconds=case["stages"].get(s, {}).get("ages", {}).get(ROOT,10.0))).isoformat()
                for s in ("initial", "release")}
            try:
                age = (times(protocol)["release"] - datetime.fromisoformat(answer["observed_at"])).total_seconds()
            except (TypeError, ValueError):
                age = -1
            A = (answer["key"] == ROOT and answer["observed_at"] in allowed_times and 0 <= age <= 120
                 and answer["text"] == f'근권 온도 {answer["value"]}°C'
                 and answer["historical"] is None and answer["reason"] is None)
        else:
            setting = case["stages"].get("release", {})
            reason = ("snapshot_unavailable" if setting.get("snapshot_error") else
                      "read_error" if ROOT in setting.get("errors", []) else
                      "missing" if ROOT in setting.get("missing", []) else
                      "stale" if setting.get("ages", {}).get(ROOT,10) > 120 else None)
            A = (reason is not None and answer["reason"] == reason and answer["value"] is None
                 and answer["observed_at"] is None and answer["text"].startswith(f"현재 근권 온도 확인 불가 ({reason})"))
            history = answer.get("historical")
            expected_text = f"현재 근권 온도 확인 불가 ({reason})"
            if history:
                initial = case["stages"].get("initial", {})
                A = (A and not initial.get("snapshot_error") and ROOT not in initial.get("errors", [])
                     and ROOT not in initial.get("missing", []) and "이전 관측" in answer["text"]
                     and history["value"] == initial.get("values", {}).get(ROOT,19.0)
                     and history["observed_at"] == (T0-timedelta(seconds=initial.get("ages", {}).get(ROOT,10))).isoformat())
                expected_text += f" · 이전 관측 {history['value']}°C ({history['observed_at']})"
            A = A and answer["key"] == ROOT and answer["text"] == expected_text

    def action_ok(target):
        actual = record.get("actions", {}).get(target)
        want = expected[target]
        if not actual or any(actual[k] != want[k] for k in ("status", "applied")):
            return False
        # Equivalent non-required decisions must not lose points for a label.
        allowed_reasons = {want["reason"]}
        if want["status"] == "not_needed" and want["reason"] == "condition_not_met":
            allowed_reasons.add("no_action")
        if actual["reason"] not in allowed_reasons:
            return False
        if not want["applied"]:
            return actual["reason"] is not None and not (actual.get("execution") or {}).get("applied",False)
        candidate = actual.get("candidate") or {}
        is_cooling = target == "cooling"
        state = case["stages"].get("execution", {})
        source_key = ROOT if is_cooling else HUMIDITY
        permission = PERMIT_COOLER if is_cooling else PERMIT_FAN
        valid_source = (not state.get("snapshot_error") and source_key not in state.get("errors", [])
                        and source_key not in state.get("missing", [])
                        and state.get("ages", {}).get(source_key,10) <= 120
                        and state.get("values", {}).get(permission,True) is True)
        input_value = state.get("values", {}).get(source_key,19.0 if is_cooling else 85.0)
        valid_source = valid_source and input_value > (18 if is_cooling else 80)
        execution = actual.get("execution") or {}
        return bool(valid_source and candidate.get("target") == ("root_zone_chiller" if is_cooling else "circulation_fan")
                    and candidate.get("zone_id") == "zone-a"
                    and candidate.get("action") == ("set_cooling_enabled" if is_cooling else "set_circulation_fan_percent")
                    and (candidate.get("value") is True if is_cooling else type(candidate.get("value")) in (int,float) and candidate["value"] == 25)
                    and actual["executor_reached"] and execution.get("dry_run") is True
                    and execution.get("applied") is True and execution.get("failure_code") is None)

    A,C,F = int(A),int(action_ok("cooling")),int(action_ok("fan"))
    return {"A":A, "C":C, "F":F, "S":int(A and C and F and record["completed"] and record["exception"] is None)}


def execute(protocol, registry):
    results = []
    for case in protocol["cases"]:
        for policy in protocol["policies"]:
            source = ScriptedSource(case)
            record = plain(run_joint(source, policy, registry, times(protocol)))
            record.update({"case_id":case["id"], "category":case["category"], "source_reads":source.reads})
            record["scores"] = score(record, case, protocol)
            results.append(record)
    return results


def compare_historical(protocol, registry, records):
    checks = []
    for case in protocol["cases"]:
        if case["category"] == "permission":
            continue  # Old research core has no permission-field contract.
        reference = next(r for r in records if r["case_id"] == case["id"] and r["policy"] == "legacy")
        for target in ("cooling", "fan"):
            source = ScriptedSource(case)

            class Reader:
                calls = 0
                def read(self):
                    stage = ("initial", "release", "execution")[min(self.calls,2)]
                    self.calls += 1
                    obs = collect(source, stage, times(protocol)[stage])
                    _legacy_read_guard(obs)
                    return _snapshot(obs, target, self.calls)

            is_cooling = target == "cooling"
            factory = action_factory({"zone_id":"zone-a", "action_contract":{"initial_candidate":{
                "action":"set_cooling_enabled" if is_cooling else "set_circulation_fan_percent",
                "target":"root_zone_chiller" if is_cooling else "circulation_fan", "value":True if is_cooling else 25.0}}})
            generator = RecordingGenerator(factory)
            try:
                old = ExperimentEngine(Reader(),generator,DryRunExecutor()).run(
                    SR_SELECTIVE_REVISION, question="fixed fixture question",
                    related_state=related_state_spec("root-zone-cooling-control-v1" if is_cooling else "humidity-fan-control-v1",registry))
                completed, applied = True, old.applied
            except SnapshotUnavailable:
                completed, applied = False, False
            agreed = completed == reference["completed"] and applied == reference.get("actions",{}).get(target,{}).get("applied",False)
            checks.append({"case_id":case["id"],"target":target,"old_completed":completed,"old_applied":applied,"agreed":agreed})
    return checks


def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args(argv)
    args.output.mkdir(parents=True,exist_ok=False)
    protocol_path=RESEARCH/'protocols/availability-joint.v1.json'
    registry_path=RESEARCH/'protocols/related-state.v2.json'
    protocol=json.loads(protocol_path.read_text(encoding='utf-8'))
    registry=json.loads(registry_path.read_text(encoding='utf-8'))
    sources=sorted((RESEARCH/'src').rglob('*.py'))+sorted((RESEARCH/'tests').rglob('*.py'))+[protocol_path,registry_path,RESEARCH/'pyproject.toml']
    hashes={str(p.relative_to(RESEARCH)):sha256(p.read_bytes()).hexdigest() for p in sources}
    manifest=dict(protocol=protocol,source_hashes=hashes,started_at=datetime.now(timezone.utc).isoformat(),
                  environment=dict(python=platform.python_version(),os=platform.platform(),machine=platform.machine(),dependencies='Python standard library'),
                  repository_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=RESEARCH,text=True).strip(),
                  registered_cases=len(protocol['cases']),measured_runs=len(protocol['cases'])*2,
                  exploratory_predecessor='availability-followup-20261004; availability-audit-20261004-reviewed')
    (args.output/'manifest.json').write_text(dump(manifest),encoding='utf-8')
    records=execute(protocol,registry)
    (args.output/'records.json').write_text(dump(records),encoding='utf-8')
    compatibility=compare_historical(protocol,registry,records)
    replay=execute(protocol,registry)==records
    summary={p:{k:sum(r['scores'][k] for r in records if r['policy']==p) for k in 'ACFS'} for p in protocol['policies']}
    transitions={}
    for k in 'ACFS':
        pairs=[tuple(next(r['scores'][k] for r in records if r['case_id']==c['id'] and r['policy']==p) for p in protocol['policies']) for c in protocol['cases']]
        transitions[k]={'fail_to_pass':pairs.count((0,1)),'pass_to_fail':pairs.count((1,0)),'both_pass':pairs.count((1,1)),'both_fail':pairs.count((0,0))}
    result=dict(N=len(protocol['cases']),metrics=summary,paired_counts=transitions,replay_equal=replay,historical_compatibility=compatibility)
    (args.output/'summary.json').write_text(dump(result),encoding='utf-8')
    assert replay, 'Replay differs; do not publish results'
    assert all(c['agreed'] for c in compatibility), 'Historical compatibility failed'
    assert all(sha256((RESEARCH/name).read_bytes()).hexdigest()==v for name,v in hashes.items())
    with ZipFile(args.output/'source-snapshot.zip','w',ZIP_DEFLATED) as archive:
        for name in hashes:
            archive.write(RESEARCH/name,name)
    with (args.output/'scores.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.writer(stream)
        writer.writerow(['case_id','category','policy','answer','cooling','fan','all_pass','exception'])
        for row in records:
            writer.writerow([row['case_id'],row['category'],row['policy'],*[row['scores'][k] for k in 'ACFS'],bool(row['exception'])])
    metric_names={'A':'답변의 현재 값·확인 불가 표시','C':'냉각 조절 및 보류 판단','F':'독립적인 팬 조절 판단','S':'세 항목 모두 충족'}
    table=''.join(f'<tr><td>{metric_names[k]}</td><td>{summary["legacy"][k]}/{result["N"]}</td><td>{summary["guarded"][k]}/{result["N"]}</td><td>{transitions[k]["fail_to_pass"]} / {transitions[k]["pass_to_fail"]}</td></tr>' for k in 'ACFS')
    cases_html=''.join('<tr><td>'+html.escape(c['label'])+'</td>'+''.join('<td>'+str(next(r['scores']['S'] for r in records if r['case_id']==c['id'] and r['policy']==policy))+'</td>' for policy in protocol['policies'])+'</tr>' for c in protocol['cases'])
    report=f'''<!doctype html><html lang="ko"><meta charset="utf-8"><title>센서 조회 보완 실험</title>
<style>body{{font:17px/1.7 'Malgun Gothic',sans-serif;color:#20323c;max-width:1100px;margin:50px auto;padding:0 28px}}h1,h2{{color:#235e70}}table{{border-collapse:collapse;width:100%;margin:22px 0}}th,td{{padding:12px;text-align:left;border-bottom:1px solid #dce3e7}}th{{background:#edf3f5}}.note{{border-left:4px solid #327b88;padding:14px 20px;background:#f5f8f9}}</style>
<h1>센서 값을 읽지 못할 때의 답변·조절 처리</h1><p>동일한 24개 상황에서 기존 처리 원칙을 재현한 비교 구현과 보완 구현을 대조했다. 한 시험에서 답변·냉각·팬을 함께 처리했다.</p>
<div class="note">설계된 시험 집합의 기능 검증이다. 실제 농장·실물 장치·LLM 응답 품질을 측정한 결과가 아니다.</div>
<h2>시험 환경과 횟수</h2><p>Windows · Python {platform.python_version()} · 표준 라이브러리 · 결정론적 입력 · DryRunExecutor. 기준 온도 19°C, 습도 85%, 팬 조정안 25%. 냉각은 18°C 초과, 팬은 습도 80% 초과에서 다른 조건도 만족해야 허용한다. 자료 유효기간은 120초 이하이다.</p>
<p>24개 상황 × 2방식 = 본 측정 48회. 동일 입력 재실행 48회는 재현성 점검이며 표본에 더하지 않았다. 기존 엔진과 단일 장치 동작 대조 44회도 별도 검증이다. 합계 140회이며 단위시험과 개발 중 실행은 제외한다.</p>
<h2>판정 결과</h2><p>각 항목은 미리 지정한 기대 값·실행 여부·보류 이유와 일치하면 1점이다. 답변은 현재 값과 관측 시각, 확인 불가 표시, 과거 값의 구분까지 확인한다. 세 점수의 곱이 1인 경우만 시험 전체 통과로 센다. 예외로 종료한 시험도 분모 24에 남긴다.</p>
<table><tr><th>판정 항목</th><th>기존 원칙 비교 구현</th><th>보완 구현</th><th>실패→통과 / 통과→실패</th></tr>{table}</table>
<h2>해석과 적용 범위</h2><p>개별 센서 읽기 실패와 전체 조회 실패를 구분했다. 온도만 읽지 못하면 냉각을 보류하면서 정상 습도 자료에 근거한 팬 조절을 유지한다. 전체 조회 실패이면 둘 다 보류한다. 실행 직전 오류와 허용 철회도 별도로 시험했다.</p>
<p>24개는 조건 분할과 경계값으로 선정한 사례 수이며, 무작위 독립 표본이 아니다. 통과 비율을 실제 환경의 성공 확률로 일반화하거나 신뢰구간·유의확률을 붙이지 않는다. 비교 구현은 공통의 두 장치 처리 구조를 사용하며, 과거 엔진 자체를 수정한 것이 아니다. 권한 조건을 제외한 22상황 × 2장치의 종료·실행 여부가 과거 엔진과 일치했다.</p>
<p>실험은 탐색 검토 뒤 설계한 후속 기능 검증이다. 코드와 수작업 기대 결과를 개발 단계에서 함께 검토했으므로 맹검 평가가 아니다. 채점은 정형 출력용이며 자연어 의미 판정 성능을 뜻하지 않는다. 배포 API 연결·실물 실행·동시성 및 원자성은 별도 검증이 필요하다.</p>
<h2>상황별 전체 통과</h2><table><tr><th>시험 상황</th><th>기존 원칙</th><th>보완 구현</th></tr>{cases_html}</table>
<h2>재현 자료</h2><p><a href="manifest.json">환경·사전 기대 결과·소스 해시</a> · <a href="records.json">입력 및 처리 기록</a> · <a href="scores.csv">항목별 점수</a> · <a href="summary.json">재현성 및 과거 엔진 대조</a> · <a href="source-snapshot.zip">실행 소스 보관본</a></p></html>'''
    (args.output/'report.html').write_text(report,encoding='utf-8')
    (args.output/'sha256.json').write_text(dump({p.name:sha256(p.read_bytes()).hexdigest() for p in args.output.iterdir()}),encoding='utf-8')
    print(dump({'N':result['N'],'metrics':summary,'paired_counts':transitions,'replay_equal':replay,'compatibility':sum(c['agreed'] for c in compatibility),'compatibility_total':len(compatibility)}))


if __name__=='__main__':
    main()
