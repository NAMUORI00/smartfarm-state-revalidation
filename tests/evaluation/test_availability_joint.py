from copy import deepcopy
import json
import pytest

from smartfarm_state_revalidation.availability import ROOT, HUMIDITY, collect
from smartfarm_state_revalidation.evaluation.availability_joint import (
    RESEARCH, ScriptedSource, T0, execute, compare_historical, score,
)


@pytest.fixture(scope='module')
def bundle():
    p = json.loads((RESEARCH/'protocols/availability-joint.v1.json').read_text(encoding='utf-8'))
    registry = json.loads((RESEARCH/'protocols/related-state.v2.json').read_text(encoding='utf-8'))
    return p, registry, execute(p, registry)


def test_joint_case_accounting_and_historical_compatibility(bundle):
    p, registry, rows = bundle
    assert len({c['id'] for c in p['cases']}) == 24
    assert len(rows) == 48
    assert all(r['scores']['S'] for r in rows if r['policy'] == 'guarded')
    compatibility = compare_historical(p, registry, rows)
    assert len(compatibility) == 44 and all(c['agreed'] for c in compatibility)


def test_partial_error_is_readable_and_does_not_stop_independent_fan(bundle):
    p, _, rows = bundle
    row = next(r for r in rows if r['case_id']=='field_error_release' and r['policy']=='guarded')
    assert row['actions']['cooling']['applied'] is False
    assert row['actions']['fan']['applied'] is True
    assert any(e.get('key')==HUMIDITY and e['stage']=='release' and e['outcome']=='returned' for e in row['source_reads'])
    assert set(row['release_actions']) == {'cooling','fan'}
    whole = next(r for r in rows if r['case_id']=='snapshot_error_release' and r['policy']=='guarded')
    assert not any(a['applied'] for a in whole['actions'].values())


def test_recovery_does_not_resurrect_held_plan(bundle):
    row = next(r for r in bundle[2] if r['case_id']=='field_error_then_recovery' and r['policy']=='guarded')
    assert row['actions']['cooling']['status']=='hold'
    assert not row['actions']['cooling']['executor_reached']


@pytest.mark.parametrize('mutation,metric', [
    ('wrong_text','A'),('bad_time','A'),('wrong_cooler','C'),
    ('wrong_fan_value','F'),('unapplied','C'),('wrong_zone','F'),
])
def test_judge_rejects_corrupted_results(bundle, mutation, metric):
    p, _, rows = bundle
    row = deepcopy(next(r for r in rows if r['case_id']=='normal' and r['policy']=='guarded'))
    if mutation=='wrong_text': row['released_answer'][ROOT]['text']='근권 온도 99.0°C'
    if mutation=='bad_time': row['released_answer'][ROOT]['observed_at']='not-a-date'
    if mutation=='wrong_cooler': row['actions']['cooling']['candidate']['target']='wrong-device'
    if mutation=='wrong_fan_value': row['actions']['fan']['candidate']['value']=90
    if mutation=='unapplied': row['actions']['cooling']['execution']['applied']=False
    if mutation=='wrong_zone': row['actions']['fan']['candidate']['zone_id']='zone-b'
    assert score(row, p['cases'][0], p)[metric]==0


def test_unavailable_text_cannot_smuggle_current_value(bundle):
    p, _, rows = bundle
    case = next(c for c in p['cases'] if c['id']=='missing_release')
    row = deepcopy(next(r for r in rows if r['case_id']==case['id'] and r['policy']=='guarded'))
    row['released_answer'][ROOT]['text'] += ' 현재 온도는 19°C입니다.'
    assert score(row,case,p)['A']==0


def test_semantically_identical_no_action_labels_are_equivalent(bundle):
    rows=bundle[2]
    for cid,metric in [('threshold_equal','C'),('humidity_equal','F')]:
        assert all(r['scores'][metric] for r in rows if r['case_id']==cid)


def test_unexpected_programming_errors_are_not_hidden():
    class Broken(ScriptedSource):
        def read(self,key): raise RuntimeError('implementation bug')
    with pytest.raises(RuntimeError,match='implementation bug'):
        collect(Broken({'stages':{}}),'initial',T0)
