"""Plan/execute API exercises real contracts without contacting providers."""
import pytest


@pytest.mark.parametrize('value', [
    'data:image/png;base64,PRIVATE', 'https://user:private@example.com/a',
    'https://example.com/a?token=private', 'HTTPS://user:private@example.com/a',
    'HTTPS://example.com/a?token=private', 'Bearer private',
    '{"api_key": "private"}',
    r'{"api\u005fkey":"private-probe"}',
    r'{"api\u005fkey":"[redacted]"}',
    r'"{\"api\\u005fkey\":\"private-probe\"}"',
    r'provider error: {"api\u005fkey":"PROBE_PRIVATE_VALUE"}',
    r'provider error: {"api\u005fkey":"[redacted]"}',
    r'provider error: {"safe":1} then {"api\u005fkey":"PROBE_PRIVATE_VALUE"}',
    r'provider error: {"api\u005fkey":"PROBE_PRIVATE_VALUE"',
    'passwd=private-probe', 'password="first private-probe"',
])
@pytest.mark.parametrize('location', ['params', 'prompt'])
def test_plan_rejects_sensitive_values_before_persistence(tmp_path, value, location):
    from spritewarden import workflow, events
    with pytest.raises(ValueError, match='credentials'):
        workflow.plan(tmp_path, 'spritefusion', 'generate', value if location == 'prompt' else 'robot',
                      {'nested': [{'description': value}]} if location == 'params' else {})
    assert not events.default_path(tmp_path).exists()


@pytest.mark.parametrize('provider,operation,status', [
    ('pixellab', 'create_character', 'import_only'),
    ('retrodiffusion', 'rotate', 'advisory'),
])
def test_nonexecutable_plan_is_explicit_and_refused_before_claim(tmp_path, monkeypatch, provider, operation, status):
    from spritewarden import workflow, events
    from spritewarden.generator import registry
    def forbidden(*args, **kwargs):
        pytest.fail('nonexecutable operation must not dispatch')
    monkeypatch.setattr(registry, 'dispatch', forbidden)
    plan = workflow.plan(tmp_path, provider, operation, 'robot', {})
    assert plan['capability'] == registry.capability_status(provider, operation)
    assert plan['capability']['status'] == status
    assert plan['approval'] == 'not_executable'
    result = workflow.execute(tmp_path, plan, 'outputs', authorize_spend=True)
    assert result['status'] == 'not_executable'
    assert result['capability'] == plan['capability']
    store = events.EventStore(events.default_path(tmp_path), read_only=True)
    with store._connect() as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='executions'").fetchone() is None
    assert [row['event_type'] for row in store.list_events()] == ['plan.created']
    assert not (tmp_path / 'outputs').exists()


def test_plan_is_offline_and_execution_requires_explicit_spend(tmp_path, monkeypatch):
    from spritewarden import workflow
    from spritewarden.generator import registry
    calls = []
    monkeypatch.setattr(registry, 'dispatch', lambda *a, **kw: calls.append(a))
    plan = workflow.plan(tmp_path, 'spritefusion', 'generate', 'a robot', {})
    assert plan['recipe']['operation'] == 'generate'
    result = workflow.execute(tmp_path, plan, tmp_path / 'outputs')
    assert result['status'] == 'authorization_required'
    assert calls == []


def test_plan_tampering_is_rejected_before_provider_call(tmp_path):
    from spritewarden import workflow
    plan = workflow.plan(tmp_path, 'spritefusion', 'generate', 'a robot', {})
    plan['recipe']['prompt'] = 'something else'
    with pytest.raises(ValueError, match='changed|fingerprint'):
        workflow.execute(tmp_path, plan, tmp_path / 'outputs', authorize_spend=True)


def test_execution_claim_prevents_double_spend(tmp_path, monkeypatch):
    from spritewarden import workflow
    from spritewarden.generator import registry
    from spritewarden.generator.base import GenerationOutput
    calls = []
    def dispatch(*args, **kwargs):
        calls.append(args)
        return GenerationOutput(success=True, provider='spritefusion', metadata={'test': True})
    monkeypatch.setattr(registry, 'dispatch', dispatch)
    plan = workflow.plan(tmp_path, 'spritefusion', 'generate', 'a robot', {})
    assert workflow.execute(tmp_path, plan, tmp_path / 'outputs', authorize_spend=True)['status'] == 'completed'
    assert workflow.execute(tmp_path, plan, tmp_path / 'outputs', authorize_spend=True)['status'] == 'already_executed'
    assert len(calls) == 1


def test_nested_local_inputs_are_canonical_at_dispatch(tmp_path, monkeypatch):
    from spritewarden import workflow
    from spritewarden.generator import registry
    from spritewarden.generator.base import GenerationOutput
    root = tmp_path / 'project'
    root.mkdir()
    source = root / 'source.png'
    source.write_bytes(b'bound')
    (tmp_path / 'source.png').write_bytes(b'wrong cwd')
    monkeypatch.chdir(tmp_path)
    params = {'input_image': 'source.png', 'nested': {'inputs': ['source.png'], 'source_path': 'source.png'}}
    plan = workflow.plan(root, 'spritefusion', 'edit', 'cape', params)
    expected = str(source.resolve())
    assert plan['recipe']['params'] == {'input_image': expected, 'nested': {'inputs': [expected], 'source_path': expected}}
    assert params['input_image'] == 'source.png'
    seen = []
    def dispatch(recipe, *a, **kw):
        seen.append(recipe.params)
        return GenerationOutput(success=True, provider='spritefusion')
    monkeypatch.setattr(registry, 'dispatch', dispatch)
    assert workflow.execute(root, plan, 'outputs', True)['status'] == 'completed'
    assert seen == [plan['recipe']['params']]


@pytest.mark.parametrize('config', ['spritewarden.json', 'character/pipeline.json', 'character/workflow.json', 'pipeline.json'])
def test_creating_absent_configuration_invalidates_plan(tmp_path, config, monkeypatch):
    from spritewarden import workflow
    from spritewarden.generator import registry
    def forbidden(*a, **kw):
        raise AssertionError('dispatch must not occur')
    monkeypatch.setattr(registry, 'dispatch', forbidden)
    folder = tmp_path / 'character'
    folder.mkdir()
    (folder / 'source.png').write_bytes(b'source')
    plan = workflow.plan(tmp_path, 'spritefusion', 'edit', 'cape',
                         {'input_image': 'character/source.png'}, character_dir='character')
    (tmp_path / config).write_text('{}')
    with pytest.raises(ValueError, match='dependency'):
        workflow.execute(tmp_path, plan, 'outputs', authorize_spend=True)


@pytest.mark.parametrize('key', ['input', 'start_image', 'source_image', 'image'])
def test_execution_source_aliases_are_bound(tmp_path, key):
    from spritewarden import workflow
    source = tmp_path / 'source.png'
    source.write_bytes(b'original')
    plan = workflow.plan(tmp_path, 'spritefusion', 'edit', 'cape', {key: 'source.png'})
    assert plan['recipe']['params'][key] == str(source)
    source.write_bytes(b'changed')
    with pytest.raises(ValueError, match='dependency'):
        workflow.execute(tmp_path, plan, 'outputs')


def test_changed_source_invalidates_plan_even_when_path_same(tmp_path):
    from spritewarden import workflow
    source = tmp_path / 'source.png'
    source.write_bytes(b'original')
    plan = workflow.plan(tmp_path, 'spritefusion', 'edit', 'a cape', {'input_image': str(source)})
    source.write_bytes(b'replaced')
    with pytest.raises(ValueError, match='dependency'):
        workflow.execute(tmp_path, plan, tmp_path / 'outputs', authorize_spend=True)
