"""Offline regressions for revision review at the spending boundary."""
import json
import socket
from unittest.mock import Mock

import pytest
from PIL import Image

from spritewarden import workflow_state as ws
from spritewarden.gates.records import write_record
from spritewarden.generator import registry as execution
from spritewarden.generator.base import GenerationOutput
from spritewarden.router.playbook import GenerationRecipe


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError('NETWORK DISABLED')
    monkeypatch.setattr(socket.socket, 'connect', blocked)
    monkeypatch.setattr(socket, 'create_connection', blocked)
    monkeypatch.setenv('SPRITEWARDEN_OFFLINE', '1')
    monkeypatch.setenv('SPRITEWARDEN_NO_LEARN', '1')


def candidate(root, name='south', **kwargs):
    folder = root / 'bot'
    folder.mkdir(exist_ok=True)
    image = folder / (name + '.png')
    Image.new('RGBA', (32, 32)).save(image)
    write_record(image, {'start_image': True})
    rev = ws.register_revision(root, folder, stage='anchor', files={name: image.relative_to(root).as_posix()},
        provider='spritefusion', operation='generate', expected_version=ws.inspect_state(root, folder)['version'], **kwargs)
    return folder, image, rev


def decide(root, folder, rev, decision='accepted'):
    ws.record_decision(root, folder, revision_id=rev['revision_id'], decision=decision,
        actor='owner', reason='review', expected_version=ws.inspect_state(root, folder)['version'])


def dispatch(root, image, monkeypatch, force=False):
    driver = Mock()
    driver.generate.return_value = GenerationOutput(success=False, provider='spritefusion', error='offline sentinel')
    monkeypatch.setattr(execution, 'get_driver', lambda *a, **kw: driver)
    recipe = GenerationRecipe(provider='spritefusion', operation='direction-set', prompt='', size=64,
        canvas_size=64, perspective='low top-down', params={'input_image': str(image)})
    result = execution.dispatch(recipe, root / 'out', project_root=root, register=False, force_ungated=force)
    return result, driver


@pytest.mark.parametrize('force', [False, True])
def test_dispatch_honors_revision_rejection_without_sidecar_rejection(tmp_path, monkeypatch, force):
    folder, image, rev = candidate(tmp_path)
    decide(tmp_path, folder, rev, 'rejected')
    assert not ws.inspect_state(tmp_path, folder)['readiness'][rev['revision_id']]['ready']
    result, driver = dispatch(tmp_path, image, monkeypatch, force)
    driver.generate.assert_not_called()
    assert 'rejected' in result.error


def promote(root, folder, rev):
    decide(root, folder, rev)
    ws.promote(root, folder, revision_id=rev['revision_id'], actor='owner',
        expected_version=ws.inspect_state(root, folder)['version'])


def test_dispatch_refuses_unselected_registered_revision_even_forced(tmp_path, monkeypatch):
    folder, image, rev = candidate(tmp_path)
    decide(tmp_path, folder, rev)
    result, driver = dispatch(tmp_path, image, monkeypatch, True)
    driver.generate.assert_not_called()
    assert 'selection' in result.error


def test_new_selected_bytes_are_not_poisoned_by_old_rejection(tmp_path, monkeypatch):
    folder, image, old = candidate(tmp_path)
    decide(tmp_path, folder, old, 'rejected')
    Image.new('RGBA', (32, 32), (255, 0, 0, 255)).save(image)
    write_record(image, {'start_image': True})
    new = ws.register_revision(tmp_path, folder, stage='anchor', files={'south': 'bot/south.png'},
        provider='spritefusion', operation='generate', expected_version=2)
    promote(tmp_path, folder, new)
    result, driver = dispatch(tmp_path, image, monkeypatch, True)
    driver.generate.assert_called_once()
    assert result.error == 'offline sentinel'


def test_dispatch_checks_recursive_dependency_readiness(tmp_path, monkeypatch):
    folder, anchor_image, anchor = candidate(tmp_path)
    promote(tmp_path, folder, anchor)
    views = folder / 'views'
    views.mkdir()
    image = views / 'south.png'
    Image.new('RGBA', (32, 32)).save(image)
    write_record(image, {'views': True, 'start_image': True})
    rev = ws.register_revision(tmp_path, folder, stage='views', files={'south': 'bot/views/south.png'},
        provider='spritefusion', operation='direction-set', expected_version=3,
        expected_facings=['south'], dependencies={'anchor': anchor['revision_id']})
    promote(tmp_path, folder, rev)
    state = ws.inspect_state(tmp_path, folder)
    assert state['readiness'][anchor['revision_id']]['ready']
    assert state['readiness'][rev['revision_id']]['ready']
    anchor_image.write_bytes(b'changed ancestor')
    result, driver = dispatch(tmp_path, image, monkeypatch, True)
    driver.generate.assert_not_called()
    assert 'stale dependency' in result.error


def test_scoped_exception_cannot_waive_absent_required_evidence(tmp_path):
    folder = tmp_path / 'bot'
    folder.mkdir()
    image = folder / 'south.png'
    Image.new('RGBA', (32, 32)).save(image)
    write_record(image, {'start_image': False, 'symmetry': {'passed': False, 'status': 'fail'}},
        params={'required_evidence': ['start_image', 'symmetry', 'pixel_grid']})
    rev = ws.register_revision(tmp_path, folder, stage='anchor', files={'south': 'bot/south.png'},
        provider='spritefusion', operation='generate', expected_version=0)
    ws.record_decision(tmp_path, folder, revision_id=rev['revision_id'], decision='accepted_with_exception',
        gate='symmetry', operation='generate', actor='owner', reason='intentional asymmetry', expected_version=1)
    with pytest.raises(ValueError, match='pixel_grid'):
        ws.promote(tmp_path, folder, revision_id=rev['revision_id'], actor='owner', expected_version=2)
    ready = ws.inspect_state(tmp_path, folder)['readiness'][rev['revision_id']]
    assert not ready['ready']
    assert any('pixel_grid' in reason for reason in ready['reasons'])


@pytest.mark.parametrize('field,value', [('anchor_facing', 'north'), ('new_design_constraint', {'cape': False}),
    ('rotation_provider', 'spritecook'), ('expected_facings', ['north'])])
def test_semantic_pipeline_edits_invalidate_promoted_revision(tmp_path, field, value):
    folder = tmp_path / 'bot'
    folder.mkdir()
    pipeline = folder / 'pipeline.json'
    pipeline.write_text(json.dumps({'anchor_facing': 'south'}))
    folder, image, rev = candidate(tmp_path)
    promote(tmp_path, folder, rev)
    assert ws.inspect_state(tmp_path, folder)['readiness'][rev['revision_id']]['ready']
    data = json.loads(pipeline.read_text())
    data[field] = value
    pipeline.write_text(json.dumps(data))
    ready = ws.inspect_state(tmp_path, folder)['readiness'][rev['revision_id']]
    assert not ready['ready']
    assert 'design changed' in ready['reasons']


def test_dispatch_rechecks_review_after_attempt_intent(tmp_path, monkeypatch):
    from spritewarden import events
    folder, image, rev = candidate(tmp_path)
    promote(tmp_path, folder, rev)
    emit = events.emit
    def reject_at_intent(kind, **kwargs):
        result = emit(kind, **kwargs)
        if kind == 'attempt.started':
            decide(tmp_path, folder, rev, 'rejected')
        return result
    monkeypatch.setattr(events, 'emit', reject_at_intent)
    result, driver = dispatch(tmp_path, image, monkeypatch, True)
    driver.generate.assert_not_called()
    assert 'rejected' in result.error
