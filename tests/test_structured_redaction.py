"""Bounded structural redaction at untrusted text and plan boundaries."""
import json

import pytest

from spritewarden import events, workflow


@pytest.mark.parametrize('text', [
    r'provider error: "api\u005fkey": "PROBE_PRIVATE_VALUE"',
    r'provider error: "api\u005fkey" : "first PROBE_PRIVATE_VALUE last',
])
def test_truncated_object_with_surviving_key_token_fails_closed(text):
    assert 'PROBE_PRIVATE_VALUE' not in events.sanitize(text)


_SECRET_FRAGMENT = r'{"api\u005fkey": "first PROBE_PRIVATE_VALUE last"}'


@pytest.mark.parametrize('text', [
    'provider error: ' + _SECRET_FRAGMENT + ' suffix',
    'first {"safe":1} next ' + _SECRET_FRAGMENT + ' last [1,2]',
    'provider error: ' + json.dumps({'outer': [_SECRET_FRAGMENT]}),
    'provider error: ' + json.dumps(json.dumps(_SECRET_FRAGMENT)),
    'provider error: ' + json.dumps({'outer': [{'passwd': 'first " PROBE_PRIVATE_VALUE last'}]}),
    'provider error: "api\\u005fkey": "PROBE_PRIVATE_VALUE"',
])
def test_fragment_matrix_cannot_reach_ledger_spool_or_plan(tmp_path, text, caplog, capsys):
    store = events.EventStore(tmp_path / 'matrix.db')
    store.append('operation.failed', payload={'message': text})
    assert 'PROBE_PRIVATE_VALUE' not in json.dumps(store.list_events())
    receipt = events._spool({'event_id': 'probe', 'payload': events.sanitize({'message': text})}, tmp_path)
    assert 'PROBE_PRIVATE_VALUE' not in receipt.read_text(encoding='utf-8')
    for location in ('prompt', 'params'):
        with pytest.raises(ValueError, match='credentials') as exc:
            workflow.plan(tmp_path, 'spritefusion', 'generate',
                          text if location == 'prompt' else 'robot',
                          {'nested': [text]} if location == 'params' else {})
        assert 'PROBE_PRIVATE_VALUE' not in str(exc.value)
    assert not events.default_path(tmp_path).exists()
    captured = capsys.readouterr()
    assert 'PROBE_PRIVATE_VALUE' not in caplog.text + captured.out + captured.err


@pytest.mark.parametrize('text', [
    'plain provider response: retry later',
    '123 null true false',
    'prefix {"safe":[1, true, null, "hello"]} middle [2,3] suffix "world"',
    'prefix ' + json.dumps(json.dumps({'safe': ['hello', 1]})) + ' suffix',
    'draw a "small robot" with a blue cape',
    'passwordless is an ordinary word',
])
def test_safe_embedded_text_is_preserved_including_plans(tmp_path, text):
    assert events.sanitize(text) == text
    assert events.sanitize(text, reject_sensitive=True) == text
    plan = workflow.plan(tmp_path, 'spritefusion', 'generate', 'robot', {'description': text})
    assert plan['recipe']['params']['description'] == text


def test_total_text_budget_is_shared_across_values():
    assert events.sanitize(['x' * 20000] * 11) == '[redacted]'


def test_fragment_decode_work_is_bounded(monkeypatch):
    decoder = json.JSONDecoder()
    calls = []

    class CountingDecoder:
        def raw_decode(self, text, idx=0):
            calls.append(idx)
            return decoder.raw_decode(text, idx)

    monkeypatch.setattr(events, '_JSON_DECODER', CountingDecoder())
    text = '{} ' * 1000 + 'PROBE_PRIVATE_VALUE'
    assert events.sanitize(text) == '[redacted]'
    assert len(calls) <= 256


@pytest.mark.parametrize('kind', ['deep-json', 'deep-python', 'cycle', 'long', 'wide'])
def test_resource_exhaustion_fails_closed(kind):
    if kind == 'deep-json':
        value = '[' * 2000 + '"PROBE_PRIVATE_VALUE"' + ']' * 2000
    elif kind == 'deep-python':
        value = 'PROBE_PRIVATE_VALUE'
        for _ in range(100):
            value = [value]
    elif kind == 'cycle':
        value = ['PROBE_PRIVATE_VALUE']
        value.append(value)
    elif kind == 'long':
        value = 'PROBE_PRIVATE_VALUE' + 'x' * 20001
    else:
        value = ['PROBE_PRIVATE_VALUE'] * 10001
    assert events.sanitize(value) == '[redacted]'


def test_plan_resource_exhaustion_rejected_before_persistence(tmp_path):
    params = {}
    params['nested'] = params
    with pytest.raises(ValueError, match='credentials'):
        workflow.plan(tmp_path, 'spritefusion', 'generate', 'robot', params)
    assert not events.default_path(tmp_path).exists()
