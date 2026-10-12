import json
import pytest
from spritewarden import workflow_state as ws


def test_migration_is_read_only_and_does_not_guess(tmp_path):
    folder = tmp_path / 'bot'; folder.mkdir()
    legacy = {'anchor': 'old.png', 'rotation_pick': {'game_views': 'new'}, 'notes': 'approved new'}
    path = folder / 'pipeline.json'; path.write_text(json.dumps(legacy))
    before = path.read_bytes()
    proposal = ws.propose_migration(tmp_path, folder)
    assert proposal['requires_review'] is True
    assert proposal['legacy'] == legacy
    assert proposal['current'] == {}
    assert path.read_bytes() == before
    assert ws.inspect_state(tmp_path, folder)['version'] == 0


def candidate(tmp_path, **kw):
    from spritewarden.gates.records import write_record
    folder = tmp_path / 'bot'; folder.mkdir(exist_ok=True)
    image = folder / 'south.png'; image.write_bytes(b'pixel bytes')
    write_record(image, {'start_image': {'passed': True}})
    return folder, ws.register_revision(tmp_path, folder, stage='anchor', files={'south': 'bot/south.png'},
        provider='pixellab', operation='create', native_ids={'id': 'native-1'}, expected_version=0, **kw)


def test_revision_decision_promotion_and_conflict(tmp_path):
    folder, rev = candidate(tmp_path)
    assert rev['revision_id']
    with pytest.raises(ValueError, match='decision'):
        ws.promote(tmp_path, folder, revision_id=rev['revision_id'], expected_version=1, actor='owner')
    ws.record_decision(tmp_path, folder, revision_id=rev['revision_id'], decision='accepted', actor='owner',
                       reason='looks right', expected_version=1)
    state = ws.promote(tmp_path, folder, revision_id=rev['revision_id'], expected_version=2, actor='owner')
    assert state['current']['anchor'] == rev['revision_id']
    data = json.loads((folder / 'pipeline.json').read_text())
    assert data['anchor'] == 'bot/south.png'
    assert data['identity']['pixellab']['id'] == 'native-1'
    with pytest.raises(ws.StateConflict):
        ws.record_decision(tmp_path, folder, revision_id=rev['revision_id'], decision='rejected', actor='owner',
                           reason='stale tab', expected_version=1)
    (folder / 'south.png').write_bytes(b'changed')
    assert ws.inspect_state(tmp_path, folder)['readiness'][rev['revision_id']]['ready'] is False
    assert ws.inspect_state(tmp_path, folder)['revisions'][rev['revision_id']] == rev


def test_views_exact_set_scoped_exception_and_design_invalidation(tmp_path):
    from spritewarden.gates.records import write_record
    folder, anchor = candidate(tmp_path)
    ws.record_decision(tmp_path, folder, revision_id=anchor['revision_id'], decision='accepted', actor='owner', reason='yes', expected_version=1)
    ws.promote(tmp_path, folder, revision_id=anchor['revision_id'], expected_version=2, actor='owner')
    views = folder / 'views'; views.mkdir()
    files = {}
    for face in ['south', 'north']:
        path = views / (face + '.png'); path.write_bytes(face.encode())
        write_record(path, {'views': {'passed': True}, 'symmetry': {'passed': False}})
        files[face] = path.relative_to(tmp_path).as_posix()
    kwargs = dict(stage='views', files=files, provider='pixellab', operation='rotate', expected_version=3,
                  dependencies={'anchor': anchor['revision_id']}, native_ids={'id': 'views-id'})
    with pytest.raises(ValueError, match='facings'):
        ws.register_revision(tmp_path, folder, expected_facings=['south'], **kwargs)
    rev = ws.register_revision(tmp_path, folder, expected_facings=['south', 'north'], **kwargs)
    ws.record_decision(tmp_path, folder, revision_id=rev['revision_id'], decision='accepted_with_exception',
        actor='owner', reason='intentional tool asymmetry', gate='symmetry', operation='rotate', expected_version=4)
    extra = views / 'east.png'; extra.write_bytes(b'unselected provider candidate')
    with pytest.raises(ValueError, match='unselected'):
        ws.promote(tmp_path, folder, revision_id=rev['revision_id'], actor='owner', expected_version=5)
    extra.unlink()
    state = ws.promote(tmp_path, folder, revision_id=rev['revision_id'], actor='owner', expected_version=5)
    assert state['revisions'][rev['revision_id']]['files']['south']['evidence']['gates']['symmetry']['passed'] is False
    data = json.loads((folder / 'pipeline.json').read_text())
    assert data['views_dir'] == 'bot/views'
    assert data['identity']['pixellab']['id'] == 'views-id'
    extra.write_bytes(b'unselected')
    assert not ws.inspect_state(tmp_path, folder)['readiness'][rev['revision_id']]['ready']
    extra.unlink()
    data['faces'] = {'back': 'new backpack'}
    (folder / 'pipeline.json').write_text(json.dumps(data))
    assert ws.inspect_state(tmp_path, folder)['readiness'][rev['revision_id']]['ready'] is False


def test_state_paths_cannot_escape(tmp_path):
    folder = tmp_path / 'bot'; folder.mkdir()
    with pytest.raises(ValueError):
        ws.inspect_state(tmp_path, '../outside')
    with pytest.raises(ValueError):
        ws.register_revision(tmp_path, folder, stage='anchor', files={'south': '../secret'}, provider='p', operation='o', expected_version=0)


def test_rejection_clears_current_pointer_and_status_uses_revisions(tmp_path):
    from spritewarden.enforce.config import load_config
    from spritewarden.enforce.pipeline import stage_status
    folder, rev = candidate(tmp_path)
    ws.record_decision(tmp_path, folder, revision_id=rev['revision_id'], decision='accepted', actor='owner', reason='ok', expected_version=1)
    ws.promote(tmp_path, folder, revision_id=rev['revision_id'], expected_version=2, actor='owner')
    data = json.loads((folder / 'pipeline.json').read_text())
    assert stage_status(data, load_config(tmp_path))['anchor'] is True
    from spritewarden.enforce.pipeline import check_pipeline
    assert not any(p['stage'] == 'anchor' for p in check_pipeline(data, load_config(tmp_path), folder))
    ws.record_decision(tmp_path, folder, revision_id=rev['revision_id'], decision='rejected', actor='owner', reason='correction', expected_version=3)
    data = json.loads((folder / 'pipeline.json').read_text())
    assert not data.get('anchor')
    assert stage_status(data, load_config(tmp_path))['anchor'] is False
    assert len(ws.inspect_state(tmp_path, folder)['decisions']) == 2


def test_ledger_failure_prevents_mutation(tmp_path, monkeypatch):
    from spritewarden import events
    folder, rev = candidate(tmp_path)
    old = (folder / 'pipeline.json').read_bytes()
    monkeypatch.setattr(events, 'emit', lambda *a, **k: None)
    with pytest.raises(RuntimeError, match='ledger'):
        ws.record_decision(tmp_path, folder, revision_id=rev['revision_id'], decision='accepted', actor='owner', reason='ok', expected_version=1)
    assert (folder / 'pipeline.json').read_bytes() == old


def test_legacy_save_cannot_overwrite_revision_history(tmp_path):
    from spritewarden.enforce.pipeline import save_pipeline
    folder, rev = candidate(tmp_path)
    with pytest.raises(ValueError, match='revisioned'):
        save_pipeline(folder, {'anchor': 'fake.png'})
    assert ws.inspect_state(tmp_path, folder)['revisions'][rev['revision_id']] == rev


def test_scoped_exception_can_override_aggregate_not_unavailable(tmp_path):
    from spritewarden.gates.records import write_record
    folder = tmp_path / 'bot'; folder.mkdir()
    image = folder / 'south.png'; image.write_bytes(b'pixels')
    write_record(image, {'start_image': {'passed': False}, 'symmetry': {'passed': False, 'status': 'fail'}})
    rev = ws.register_revision(tmp_path, folder, stage='anchor', files={'south': 'bot/south.png'}, provider='p', operation='create', expected_version=0)
    ws.record_decision(tmp_path, folder, revision_id=rev['revision_id'], decision='accepted_with_exception', actor='owner', reason='asymmetric tool', gate='symmetry', operation='create', expected_version=1)
    assert ws.promote(tmp_path, folder, revision_id=rev['revision_id'], actor='owner', expected_version=2)['current']['anchor'] == rev['revision_id']
    from spritewarden.enforce.pipeline import check_pipeline
    from spritewarden.enforce.config import load_config
    data = json.loads((folder / 'pipeline.json').read_text())
    assert not any(p['stage'] == 'anchor' for p in check_pipeline(data, load_config(tmp_path), folder))
    write_record(image, {'start_image': {'passed': False}, 'symmetry': {'passed': False, 'status': 'unavailable'}})
    newer = ws.register_revision(tmp_path, folder, stage='anchor', files={'south': 'bot/south.png'}, provider='p', operation='create', expected_version=3)
    with pytest.raises(ValueError, match='failed evidence'):
        ws.record_decision(tmp_path, folder, revision_id=newer['revision_id'], decision='accepted_with_exception', actor='owner', reason='not measured', gate='symmetry', operation='create', expected_version=4)
