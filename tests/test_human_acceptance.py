import json
import pytest
from spritewarden.gates import records as gr
from spritewarden.cli import main as cli_main


def main(args):
    with pytest.raises(SystemExit) as exit:
        cli_main(args)
    return exit.value.code


def view(tmp_path, name='north', **changes):
    image = tmp_path / (name + '.png')
    image.write_bytes(name.encode())
    gates = {'views': {'passed': False}, 'direction_set': {'passed': True},
             'pixel_grid': {'passed': True},
             'vision_reference': {'passed': False, 'status': 'fail', 'errors': ['invented lamp']}}
    gates.update(changes)
    gr.write_record(image, gates, params={'command': 'judge-views', 'facing': name})
    return image


def test_cli_accept_completed_artistic_failure(tmp_path, capsys):
    image = view(tmp_path)
    findings = gr.load_record(image)['gates']
    assert not gr.require_passed([image], ['views'])['ok']
    assert main(['accept', str(image), '--by', 'TEST owner', '--reason', 'Reviewed emitter side-on']) == 0
    assert gr.load_record(image)['gates'] == findings
    ready = gr.stage_readiness(image, 'views')
    assert ready['ready'], ready
    assert ready['acceptance']['actor'] == 'TEST owner'
    assert {'views', 'direction_set', 'vision_reference'} <= gr.passed_gates(image)
    assert main(['review-check', str(image), '--require', 'views']) == 0
    assert 'accepted by TEST owner' in capsys.readouterr().out


def test_directory_validates_passing_members_and_accepts_only_failures(tmp_path):
    failed = view(tmp_path)
    passed = view(tmp_path, 'east', vision_reference={'passed': True})
    record = gr.load_record(failed)
    record['dependencies'] = gr.capture_dependencies(failed, record['params'])
    gr.record_path(failed).write_text(json.dumps(record))
    before = gr.record_path(passed).read_bytes()
    assert main(['accept', str(tmp_path), '--by', 'TEST owner', '--reason', 'Reviewed whole set']) == 0
    assert gr.record_path(passed).read_bytes() == before
    assert gr.require_passed([tmp_path], ['views'])['ok']


@pytest.mark.parametrize('mutation', ['missing', 'unavailable', 'pixel', 'rejected', 'bytes', 'required'])
def test_nonwaivable_directory_member_prevents_all_writes(tmp_path, mutation):
    first = view(tmp_path, 'east')
    other = view(tmp_path, 'north')
    first_record = gr.load_record(first)
    first_record['dependencies'] = gr.capture_dependencies(first, first_record['params'])
    gr.record_path(first).write_text(json.dumps(first_record))
    assert gr.dependency_problems(first_record) == []
    record = gr.load_record(other)
    if mutation == 'missing':
        record['gates'].pop('vision_reference')
    elif mutation == 'unavailable':
        record['gates']['vision_reference']['metrics'] = {'status': 'unavailable'}
    elif mutation == 'pixel':
        record['gates']['pixel_grid']['passed'] = False
    elif mutation == 'rejected':
        record['rejected'] = {'by': 'TEST owner'}
    elif mutation == 'bytes':
        other.write_bytes(b'changed')
    else:
        record['params']['required_evidence'] = ['palette']
    gr.record_path(other).write_text(json.dumps(record))
    before = {p: gr.record_path(p).read_bytes() for p in [first, other]}
    assert main(['accept', str(tmp_path), '--by', 'TEST owner', '--reason', 'No blanket override']) == 1
    assert all(gr.record_path(p).read_bytes() == data for p, data in before.items())


def test_audit_intent_failure_prevents_acceptance(tmp_path, monkeypatch):
    from spritewarden import events
    image = view(tmp_path)
    before = gr.record_path(image).read_bytes()
    monkeypatch.setattr(events, 'emit', lambda *a, **k: None)
    assert main(['accept', str(image), '--by', 'TEST owner', '--reason', 'Reviewed']) == 1
    assert gr.record_path(image).read_bytes() == before


@pytest.mark.parametrize('dependency', ['source', 'reference', 'identity', 'pipeline', 'direction_set'])
def test_changed_dependencies_invalidate_acceptance(tmp_path, dependency):
    dep = tmp_path / 'dependency.json'
    dep.write_text('original')
    image = view(tmp_path)
    record = gr.load_record(image)
    record['dependencies'][dependency] = gr._dependency(dep, tmp_path)
    gr.record_path(image).write_text(json.dumps(record))
    assert main(['accept', str(image), '--by', 'TEST owner', '--reason', 'Reviewed']) == 0
    dep.write_text('changed')
    assert not gr.stage_readiness(image, 'views')['ready']
    assert 'views' not in gr.passed_gates(image)


def test_revisioned_acceptance_uses_authoritative_decision_not_sidecar(tmp_path):
    from spritewarden import workflow_state as ws
    (tmp_path / 'spritewarden.json').write_text('{}')
    folder = tmp_path / 'bot'
    folder.mkdir()
    anchor = folder / 'south.png'
    anchor.write_bytes(b'anchor')
    gr.write_record(anchor, {'start_image': True})
    ar = ws.register_revision(tmp_path, folder, stage='anchor', files={'south': 'bot/south.png'},
                             provider='pixellab', operation='create', expected_version=0)
    ws.record_decision(tmp_path, folder, revision_id=ar['revision_id'], decision='accepted',
                       actor='TEST owner', reason='anchor reviewed', expected_version=1)
    ws.promote(tmp_path, folder, revision_id=ar['revision_id'], expected_version=2, actor='TEST owner')
    views = folder / 'views'
    views.mkdir()
    image = view(views)
    passing = view(views, 'east', vision_reference={'passed': True})
    record = gr.load_record(image)
    record['dependencies'] = gr.capture_dependencies(image, record['params'])
    gr.record_path(image).write_text(json.dumps(record))
    rev = ws.register_revision(tmp_path, folder, stage='views', files={'north': 'bot/views/north.png', 'east': 'bot/views/east.png'},
        expected_facings=['north', 'east'], provider='pixellab', operation='rotate', expected_version=3,
        dependencies={'anchor': ar['revision_id']})
    before = gr.record_path(image).read_bytes()
    assert main(['accept', str(views), '--by', 'TEST owner', '--reason', 'Emitter reviewed']) == 0
    assert gr.record_path(image).read_bytes() == before
    state = ws.inspect_state(tmp_path, folder)
    assert state['decisions'][-1]['decision'] == 'accepted_with_exception'
    assert state['decisions'][-1]['gate'] == 'vision_reference'
    assert state['readiness'][rev['revision_id']]['ready']
    assert gr.stage_readiness(image, 'views')['ready']
    assert gr.require_passed([views], ['views'])['ok']
    assert gr.require_passed([image], ['views'])['accepted'][0]['actor'] == 'TEST owner'
    ws.promote(tmp_path, folder, revision_id=rev['revision_id'], expected_version=5, actor='TEST owner')
    ws.record_decision(tmp_path, folder, revision_id=rev['revision_id'], decision='rejected',
                       actor='TEST owner', reason='changed mind', expected_version=6)
    assert main(['accept', str(image), '--by', 'TEST owner', '--reason', 'Cannot undo rejection']) == 1
    assert not gr.stage_readiness(image, 'views')['ready']


def test_failed_pixel_direction_set_cannot_be_accepted(tmp_path):
    image = view(tmp_path, direction_set={'passed': False, 'errors': ['canvas mismatch']})
    assert main(['accept', str(image), '--by', 'TEST owner', '--reason', 'Art reviewed']) == 1
    assert not gr.load_record(image).get('human_decision')


@pytest.mark.parametrize('missing', [True, False])
def test_incomplete_pixel_contract_cannot_be_accepted(tmp_path, missing):
    image = view(tmp_path)
    record = gr.load_record(image)
    if missing:
        record['gates'].pop('direction_set')
    else:
        record['gates']['views']['status'] = 'unavailable'
    gr.record_path(image).write_text(json.dumps(record))
    assert main(['accept', str(image), '--by', 'TEST owner', '--reason', 'Reviewed']) == 1


def test_intent_race_on_passing_member_prevents_failed_member_write(tmp_path, monkeypatch):
    from spritewarden import events
    image = view(tmp_path)
    passing = view(tmp_path, 'east', views={'passed': True}, vision_reference={'passed': True})
    record = gr.load_record(image)
    record['dependencies'] = gr.capture_dependencies(image, record['params'])
    gr.record_path(image).write_text(json.dumps(record))
    before = gr.record_path(image).read_bytes()
    real_emit = events.emit
    def emit(kind, **kwargs):
        result = real_emit(kind, **kwargs)
        if kind == 'human.decision.intent':
            evidence = gr.load_record(passing)
            evidence['gates'].pop('vision_reference')
            gr.record_path(passing).write_text(json.dumps(evidence))
        return result
    monkeypatch.setattr(events, 'emit', emit)
    assert main(['accept', str(tmp_path), '--by', 'TEST owner', '--reason', 'Reviewed']) == 1
    assert gr.record_path(image).read_bytes() == before


def test_public_backend_rejects_other_gate_even_for_passing_target(tmp_path):
    from spritewarden.gates.acceptance import accept
    image = view(tmp_path, views={'passed': True}, vision_reference={'passed': True})
    with pytest.raises(ValueError, match='vision_reference'):
        accept([image], actor='TEST owner', reason='Reviewed', gate='pixel_grid')


def test_public_active_handles_absent_evidence(tmp_path):
    from spritewarden.gates.acceptance import active
    assert active(tmp_path / 'missing.png', None) is None


def test_bare_failed_flag_is_not_completed_artistic_evidence(tmp_path):
    image = view(tmp_path, vision_reference={'passed': False})
    assert main(['accept', str(image), '--by', 'TEST owner', '--reason', 'Reviewed']) == 1


def test_not_applicable_pixel_set_is_not_a_completed_pass(tmp_path):
    image = view(tmp_path, direction_set={'passed': False, 'status': 'not_applicable'})
    assert main(['accept', str(image), '--by', 'TEST owner', '--reason', 'Reviewed']) == 1


def test_changed_findings_invalidate_acceptance(tmp_path):
    image = view(tmp_path)
    assert main(['accept', str(image), '--by', 'TEST owner', '--reason', 'Reviewed']) == 0
    record = gr.load_record(image)
    record['gates']['vision_reference']['errors'].append('new finding')
    gr.record_path(image).write_text(json.dumps(record))
    assert not gr.stage_readiness(image, 'views')['ready']
