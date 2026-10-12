"""Evidence contracts shared by CLI, MCP and transition preflight (offline)."""
import json
from pathlib import Path

import pytest

from spritewarden.gates import records


@pytest.fixture
def image(tmp_path, monkeypatch):
    monkeypatch.setenv('SPRITEWARDEN_LEARNING_DB', str(tmp_path / 'learning.db'))
    monkeypatch.setenv('SPRITEWARDEN_DB', str(tmp_path / 'catalog.db'))
    (tmp_path / 'spritewarden.json').write_text('{}')
    p = tmp_path / 'east.png'
    p.write_bytes(b'image revision')
    return p


@pytest.mark.parametrize('command', ['judge-views', 'judge_direction_set'])
def test_stage_readiness_rejects_partial_views(image, command):
    records.write_record(image, {'direction_set': True, 'views': False,
                                'vision_reference': False}, params={'command': command})
    result = records.stage_readiness(image, 'views')
    assert not result['ready']
    assert 'direction_set' not in records.passed_gates(image)


@pytest.mark.parametrize('command', ['judge-anim', 'judge_animation'])
def test_animation_needs_source_for_both_interfaces(image, command):
    records.write_record(image, {'anim': True, 'frame_integrity': True}, params={'command': command})
    assert not records.require_passed([image], ['anim'])['ok']


@pytest.mark.parametrize('key', ['source', 'reference', 'pipeline', 'identity_path'])
def test_upstream_revision_invalidates_evidence(image, key):
    dep = image.parent / 'dependency.json'
    dep.write_text('original')
    records.write_record(image, {'still': True}, params={key: str(dep)})
    assert records.stage_readiness(image, 'still')['ready']
    dep.write_text('changed')
    result = records.stage_readiness(image, 'still')
    assert not result['ready']
    assert any('dependency' in r for r in result['reasons'])


def test_direction_set_tracks_membership_and_sibling_revision(image):
    sibling = image.parent / 'south.png'
    sibling.write_bytes(b'south')
    records.write_record(image, {'views': True, 'vision_reference': True},
                         params={'command': 'judge-views', 'direction_set': str(image.parent)})
    assert records.stage_readiness(image, 'views')['ready']
    sibling.write_bytes(b'new south')
    assert not records.stage_readiness(image, 'views')['ready']


def test_expected_dependency_revision_must_match(image):
    records.write_record(image, {'still': True}, params={'identity': {'id': 'bot', 'revision': 1}})
    assert not records.stage_readiness(image, 'still', {'identity': {'id': 'bot', 'revision': 2}})['ready']


def test_every_judgment_and_human_decision_is_preserved(image):
    from spritewarden.events import EventStore, default_path
    records.write_record(image, {'still': False})
    records.mark_rejected(image, by='reviewer', reason='wrong tool')
    records.write_record(image, {'still': True})
    events = EventStore(default_path(image.parent)).list_events()
    assert [e['event_type'] for e in events] == ['judgment.completed', 'human.decision', 'judgment.completed']
    assert events[0]['payload']['gates']['still']['passed'] is False
    assert events[2]['payload']['gates']['still']['passed'] is True
    assert records.load_record(image)['rejected']['reason'] == 'wrong tool'
    assert not records.stage_readiness(image, 'still')['ready']


def test_unavailable_is_not_visual_failure(image):
    from spritewarden.judge.models import GateResult
    card = GateResult(name='vision_reference', passed=False, summary='offline', metrics={'status': 'unavailable'})
    records.write_record(image, records.gates_from_scorecard(card))
    gate = records.load_record(image)['gates']['vision_reference']
    assert gate['status'] == 'unavailable'
    assert not records.stage_readiness(image, 'views')['ready']


def test_human_decision_fields_survive_rejudgment(image):
    records.write_record(image, {'still': True}, extra={'human_decision': {'decision': 'accepted', 'by': 'owner'}})
    records.write_record(image, {'still': False})
    assert records.load_record(image)['human_decision']['decision'] == 'accepted'


def test_central_writer_logs_mcp_style_failures(image):
    from spritewarden.gates.rejections import read_rejections
    records.write_record(image, {'still': False, 'vision_reference': {'passed': False, 'status': 'unavailable'}},
                         params={'command': 'judge_still'})
    entries = read_rejections(image.parent / 'art_source' / 'rejections.jsonl')
    assert len(entries) == 1
    assert entries[0]['failed'][1]['status'] == 'unavailable'


def test_direct_rejection_uses_event_path(image):
    from spritewarden.events import EventStore, default_path
    from spritewarden.gates.rejections import log_rejection
    log_rejection(image, by='owner', reason='no')
    events = EventStore(default_path(image.parent)).list_events()
    assert events[-1]['event_type'] == 'human.decision'
    assert events[-1]['payload']['decision'] == 'rejected'


def test_mcp_still_vision_cannot_outvote_failed_checks(image, monkeypatch):
    from types import SimpleNamespace
    from spritewarden.server import mcp_server as m
    from spritewarden.judge.models import GateResult, StillScorecard
    from PIL import Image
    Image.new('RGBA', (16, 16)).save(image)
    card = StillScorecard(path=str(image), passed=False, score=0, summary='bad',
                          gates=[GateResult(name='clipping', passed=False, summary='clipped')])
    monkeypatch.setattr(m, 'evaluate_still', lambda **kw: card)
    class Verdict:
        passed = True
        production_ready_prob = 1
        reasoning_summary = 'fine'
        def model_dump(self): return {'passed': True}
    monkeypatch.setattr(m, 'VisionJudge', lambda **kw: SimpleNamespace(orchestrate_judgment=lambda **kw: Verdict()))
    out = m.judge_still(str(image), vision=True)
    assert not out['passed']
    assert records.load_record(image)['gates']['still']['passed'] is False


def test_interfaces_capture_resolved_reference_and_identity(image, monkeypatch):
    from spritewarden.server import mcp_server as m
    from spritewarden import cli_full
    from spritewarden.judge.models import GateResult, StillScorecard
    from spritewarden.judge.vision import ReferenceComparison
    from types import SimpleNamespace
    from PIL import Image
    Image.new('RGBA', (16, 16)).save(image)
    reference = image.parent / 'anchor.png'
    Image.new('RGBA', (16, 16)).save(reference)
    monkeypatch.setattr(m, 'evaluate_still', lambda **kw: StillScorecard(path=str(image), passed=True, score=1, summary='ok', gates=[]))
    monkeypatch.setattr(m, 'VisionJudge', lambda **kw: SimpleNamespace(compare_to_reference=lambda *a, **kw: ReferenceComparison(status='match')))
    m.judge_still(str(image), reference_path=str(reference), write_gate_record=False)
    from spritewarden.events import EventStore, default_path
    event = [e for e in EventStore(default_path(image.parent)).list_events() if e['event_type'] == 'judgment.completed'][-1]
    assert event['payload']['dependencies']['reference']['sha256'] == records.sha256_of(reference)
    assert not records.record_path(image).exists()
    cli_full._write_records([image], {'still': True}, {'reference': str(reference)}, False)
    assert len([e for e in EventStore(default_path(image.parent)).list_events() if e['event_type'] == 'judgment.completed']) == 2


def test_mcp_judgment_events_share_operation_run(image, monkeypatch):
    from spritewarden.server import mcp_server as m
    from spritewarden.events import EventStore, default_path
    from spritewarden.judge.models import StillScorecard
    monkeypatch.setattr(m, 'evaluate_still', lambda **kw: StillScorecard(path=str(image), passed=True, score=1, summary='ok', gates=[]))
    m.judge_still(str(image))
    events = EventStore(default_path(image.parent)).list_events()
    assert [e['event_type'] for e in events] == ['operation.started', 'judgment.completed', 'operation.completed']
    assert len({e['run_id'] for e in events}) == 1


def test_cli_provider_override_recompiles(image, monkeypatch):
    from spritewarden import cli_full as cli
    from spritewarden.router.playbook import determine_fallback_recipe
    from spritewarden.generator.base import GenerationOutput
    from types import SimpleNamespace
    recipe = determine_fallback_recipe('bot', 'character')
    monkeypatch.setattr(cli, 'AssetCatalog', lambda: object())
    monkeypatch.setattr(cli, 'PipelineRouter', lambda **kw: SimpleNamespace(route_request=lambda **kw: SimpleNamespace(action='generate', recipe=recipe)))
    captured = []
    monkeypatch.setattr(cli, 'dispatch', lambda r, *a, **kw: captured.append(r) or GenerationOutput(success=True, provider=r.provider))
    args = SimpleNamespace(char_id=None, prompt='bot', type='character', game='test', perspective='low top-down',
                           size=64, animation=False, vehicle=False, fusion=False, provider='spritefusion', out=str(image.parent), json=True)
    assert cli.cmd_generate(args) == 0
    assert captured[0].operation == 'direction-set'
    assert captured[0].provider == 'spritefusion'


def test_catalog_identity_revision_invalidates_its_evidence(image):
    from spritewarden.repo.catalog import AssetCatalog
    from spritewarden.repo.identity import CharacterIdentity
    cat = AssetCatalog()
    ident = CharacterIdentity(id='bot', name='Bot', game='test', palette_id='luap-40', perspective='low top-down', prompt_description='one tool')
    cat.save_identity(ident)
    records.write_record(image, {'still': True}, params={'character_id': 'bot'})
    assert records.stage_readiness(image, 'still')['ready']
    ident.prompt_description = 'two tools'
    cat.save_identity(ident)
    assert not records.stage_readiness(image, 'still')['ready']


def test_semantic_stage_cannot_bypass_completeness(image):
    records.write_record(image, {'views': True}, params={'stage': 'views', 'command': 'some_new_interface'})
    assert not records.stage_readiness(image, 'views')['ready']


def test_read_only_review_can_inspect_failed_evidence(image):
    from spritewarden.server import mcp_server as m
    records.write_record(image, {'still': False})
    result = m.review_check([str(image)], read_only=True)
    assert result['ok']
    assert result['evidence'][0]['record']['gates']['still']['passed'] is False
    assert not m.review_check([str(image)])['ok']


def test_mcp_workflow_facade_is_lazy_and_requires_authorization(image):
    from spritewarden.server import mcp_server as m
    plan = m.plan_workflow(str(image.parent), 'spritefusion', 'generate', 'crate', {})
    assert plan['plan_id']
    result = m.execute_workflow(str(image.parent), plan, str(image.parent / 'out'))
    assert not result.get('success', False)
    assert 'authoriz' in json.dumps(result).lower()
    assert m.workflow_history(str(image.parent))


def test_mcp_view_evidence_has_facing_and_set_revision_even_without_sidecars(image, monkeypatch):
    from spritewarden.server import mcp_server as m
    from spritewarden.judge.models import GateResult
    from spritewarden.events import EventStore, default_path
    from PIL import Image
    for name in ('south', 'east'):
        Image.new('RGBA', (16, 16)).save(image.parent / (name + '.png'))
    monkeypatch.setattr('spritewarden.judge.check_direction_set', lambda *a, **kw: GateResult(name='direction_set', passed=True, summary='ok'))
    out = m.judge_direction_set(str(image.parent), palette=None, write_gate_record=False)
    judgments = [e for e in EventStore(default_path(image.parent)).list_events() if e['event_type'] == 'judgment.completed']
    assert len(judgments) == 2
    assert {e['payload']['params']['facing'] for e in judgments} == {'south', 'east'}
    assert all('direction_set' in e['payload']['dependencies'] for e in judgments)
    assert not list(image.parent.glob('*.gate.json'))


def test_strict_false_preserves_diagnostic_compatibility(image):
    records.write_record(image, {'views': True}, params={'command': 'judge-views'})
    assert not records.require_passed([image], ['views'])['ok']
    assert records.require_passed([image], ['views'], strict=False)['ok']


def test_cli_judge_writes_one_canonical_judgment(image):
    from PIL import Image
    from spritewarden.cli import main
    from spritewarden.events import EventStore, default_path
    Image.new('RGBA', (16, 16), (1, 2, 3, 255)).save(image)
    with pytest.raises(SystemExit):
        main(['judge-still', str(image), '--json'])
    judgments = [e for e in EventStore(default_path(image.parent)).list_events() if e['event_type'] == 'judgment.completed']
    assert len(judgments) == 1


def test_source_alias_is_complete_when_integrity_passed(image):
    source = image.parent / 'source.png'
    source.write_bytes(b'source')
    records.write_record(image, {'anim': True, 'frame_integrity': True},
                         params={'stage': 'anim', 'source_path': str(source)})
    assert records.stage_readiness(image, 'anim')['ready']


def test_cli_reference_dependency_is_captured(image, monkeypatch):
    from PIL import Image
    from spritewarden.cli import main
    monkeypatch.setenv('SPRITEWARDEN_OFFLINE', '1')
    Image.new('RGBA', (16, 16)).save(image)
    reference = image.parent / 'anchor.png'
    Image.new('RGBA', (16, 16)).save(reference)
    with pytest.raises(SystemExit):
        main(['judge-still', str(image), '--reference', str(reference), '--json'])
    assert records.load_record(image)['dependencies']['reference']['sha256'] == records.sha256_of(reference)


def test_rejection_capture_errors_are_visible(image, monkeypatch):
    from spritewarden.gates import rejections
    monkeypatch.setattr(rejections, 'describe_source', lambda *a: (_ for _ in ()).throw(OSError('blocked')))
    with pytest.warns(RuntimeWarning, match='capture'):
        assert rejections.log_rejection(image, reason='no') is None


def test_animation_source_without_integrity_evidence_is_incomplete(image):
    source = image.parent / 'source.png'
    source.write_bytes(b'source')
    records.write_record(image, {'anim': True}, params={'command': 'judge_animation', 'source': str(source)})
    assert not records.stage_readiness(image, 'anim')['ready']


def test_unavailable_status_cannot_claim_pass(image):
    records.write_record(image, {'views': True, 'vision_reference': {'passed': True, 'status': 'unavailable'}})
    result = records.stage_readiness(image, 'views')
    assert not result['ready']
    assert result['unavailable_checks'] == ['vision_reference']


def test_mcp_reference_vision_uses_pipeline_design(image, monkeypatch):
    from spritewarden.server import mcp_server as m
    from spritewarden.judge.models import GateResult
    from PIL import Image
    for name in ('south', 'east'):
        Image.new('RGBA', (16, 16)).save(image.parent / (name + '.png'))
    (image.parent / 'pipeline.json').write_text(json.dumps({'faces': {'back': 'smooth shell'}}))
    monkeypatch.setattr('spritewarden.judge.check_direction_set', lambda *a, **kw: GateResult(name='direction_set', passed=True, summary='ok'))
    seen = {}
    def compare(*args, **kw):
        seen.update(kw)
        return {'east': GateResult(name='vision_reference', passed=True, summary='same')}
    monkeypatch.setattr('spritewarden.judge.views.compare_views_to_anchor', compare)
    m.judge_direction_set(str(image.parent), palette=None, vision=True)
    assert seen['faces'] == {'back': 'smooth shell'}


def test_rejection_projection_separates_unavailable_and_inferred_provider(image):
    from spritewarden.gates.rejections import log_rejection, read_rejections, summarize
    (image.parent / 'pipeline.json').write_text(json.dumps({'rotation_provider': 'pixellab'}))
    path = log_rejection(image, command='judge_direction_set', failed=[{'name': 'vision_reference', 'status': 'unavailable'}])
    entry = read_rejections(path)[0]
    assert entry['stage'] == 'rotation'
    assert entry['provider_basis'] == 'pipeline_inference'
    assert summarize([entry])[0]['what'] == 'unavailable: vision_reference'


def test_cli_views_records_facing_without_vision(image, monkeypatch):
    from PIL import Image
    from spritewarden.cli import main
    from spritewarden.judge.models import GateResult
    for name in ('south', 'east'):
        Image.new('RGBA', (16, 16)).save(image.parent / (name + '.png'))
    monkeypatch.setattr('spritewarden.judge.check_direction_set', lambda *a, **kw: GateResult(name='direction_set', passed=True, summary='ok'))
    with pytest.raises(SystemExit):
        main(['judge-views', str(image.parent), '--no-palette', '--json'])
    assert records.load_record(image)['params']['facing'] == 'east'
    assert records.stage_readiness(image.parent / 'south.png', 'views')['ready']
    assert not records.stage_readiness(image, 'views')['ready']


def test_cli_human_rejection_is_captured_once(image):
    from spritewarden.cli import main
    from spritewarden.events import EventStore, default_path
    with pytest.raises(SystemExit):
        main(['reject', str(image), '--by', 'reviewer', '--reason', 'no'])
    decisions = [e for e in EventStore(default_path(image.parent)).list_events() if e['event_type'] == 'human.decision']
    assert len(decisions) == 1


@pytest.mark.parametrize('interface', ['cli', 'mcp'])
def test_identity_selected_view_anchor_is_a_dependency(image, monkeypatch, interface):
    from PIL import Image
    from spritewarden.cli import main
    from spritewarden.server import mcp_server as m
    from spritewarden.repo.catalog import AssetCatalog
    from spritewarden.repo.identity import CharacterIdentity
    from spritewarden.judge.models import GateResult
    folder = image.parent / 'views'
    folder.mkdir()
    for name in ('south', 'east'):
        Image.new('RGBA', (16, 16)).save(folder / (name + '.png'))
    Image.new('RGBA', (16, 16)).save(image)
    ident = CharacterIdentity(id='bot', name='Bot', game='test', palette_id='luap-40', perspective='low top-down',
                              prompt_description='bot', turnaround_stills={'south': str(image)})
    cat = AssetCatalog(); cat.save_identity(ident)
    monkeypatch.setattr(m, 'get_catalog', lambda: cat)
    monkeypatch.setattr('spritewarden.judge.check_direction_set', lambda *a, **kw: GateResult(name='direction_set', passed=True, summary='ok'))
    monkeypatch.setattr('spritewarden.judge.views.compare_views_to_anchor', lambda *a, **kw: {'east': GateResult(name='vision_reference', passed=True, summary='same')})
    if interface == 'cli':
        with pytest.raises(SystemExit):
            main(['judge-views', str(folder), '--char-id', 'bot', '--no-palette', '--vision', '--json'])
    else:
        m.judge_direction_set(str(folder), character_id='bot', palette=None, vision=True)
    record = records.load_record(folder / 'east.png')
    assert record['dependencies']['reference']['path'] == str(image.resolve())


def test_judgment_capture_without_sidecar(image):
    from spritewarden.events import EventStore, default_path
    records.write_record(image, {'still': True}, persist=False)
    assert not records.record_path(image).exists()
    assert EventStore(default_path(image.parent)).list_events()[-1]['event_type'] == 'judgment.completed'
