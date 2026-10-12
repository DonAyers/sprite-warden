"""Offline behavioral coverage for durable, sanitized workflow evidence."""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest


@pytest.mark.parametrize('text', [
    '{"api_key": "private-one", "safe": "ok"}',
    'failed response: {"password": "private-one"}',
    "{'access_token': 'private-one'}",
])
def test_sanitize_quoted_secret_keys(text):
    from spritewarden.events import sanitize
    assert 'private-one' not in sanitize(text)


@pytest.mark.parametrize('text', [
    r'{"api\u005fkey":"private-probe"}',
    json.dumps(r'{"api\u005fkey":"private-probe"}'),
    r'{"nested":[{"pass\u0077d":"private-probe"}]}',
    'passwd=private-probe',
    'password="first private-probe" safe=keep',
    r'password="first \"private-probe\" last" safe=keep',
    "cookie='first private-probe' safe=keep",
    'client_credential=private-probe',
])
def test_serialized_and_inline_secrets_never_reach_ledger(tmp_path, text):
    from spritewarden.events import EventStore, sanitize
    assert 'private-probe' not in sanitize(text)
    store = EventStore(tmp_path / 'secret.db')
    store.append('operation.failed', payload={'message': text})
    assert 'private-probe' not in json.dumps(store.list_events())


def test_prefixed_escaped_json_is_redacted_before_ledger(tmp_path):
    from spritewarden.events import EventStore, sanitize
    text = r'provider error: {"api\u005fkey": "PROBE_PRIVATE_VALUE"}'
    assert 'PROBE_PRIVATE_VALUE' not in sanitize(text)
    store = EventStore(tmp_path / 'embedded.db')
    store.append('operation.failed', payload={'message': text})
    assert 'PROBE_PRIVATE_VALUE' not in json.dumps(store.list_events())


@pytest.mark.parametrize('text', ['123', 'null', 'true', '"hello"', '{"safe": ["hello", 1]}'])
def test_safe_serialized_scalars_terminate_and_preserve_text(text):
    from spritewarden.events import sanitize
    assert sanitize(text) == text


@pytest.mark.parametrize('text', [
    r'provider error: {"api\u005fkey": "PROBE_PRIVATE_VALUE"',
    r'provider error: {"api\u005fkey": "first PROBE_PRIVATE_VALUE last}',
    r'provider error: {"api\u00ZZkey": "PROBE_PRIVATE_VALUE"}',
    r'provider error: [ {"api\u005fkey": "PROBE_PRIVATE_VALUE"}, broken ]',
    r'provider error: {"safe":{"api\u005fkey":"PROBE_PRIVATE_VALUE"}, "safe":0}',
])
def test_ambiguous_structured_fragments_fail_closed(tmp_path, text, caplog, capsys):
    from spritewarden.events import EventStore, sanitize
    assert 'PROBE_PRIVATE_VALUE' not in sanitize(text)
    store = EventStore(tmp_path / 'malformed.db')
    store.append('operation.failed', payload={'message': text})
    assert 'PROBE_PRIVATE_VALUE' not in json.dumps(store.list_events())
    assert 'PROBE_PRIVATE_VALUE' not in caplog.text
    captured = capsys.readouterr()
    assert 'PROBE_PRIVATE_VALUE' not in captured.out + captured.err


def test_events_are_append_only_correlated_and_paginated(tmp_path):
    from spritewarden.events import EventStore
    store = EventStore(tmp_path / 'events.db')
    first = store.append('attempt.started', run_id='r1', payload={'provider': 'example'})
    store.append('attempt.completed', run_id='r1', payload={'output': 'sprite.png'})
    store.append('attempt.started', run_id='r2', payload={})
    rows = store.list_events(run_id='r1')
    assert [row['event_type'] for row in rows] == ['attempt.started', 'attempt.completed']
    assert rows[0]['event_id'] == first
    assert store.list_events(run_id='r1', limit=1, offset=1) == rows[1:]
    with sqlite3.connect(store.path) as conn:
        with pytest.raises(sqlite3.DatabaseError, match='append-only'):
            conn.execute('DELETE FROM events')


def test_payload_redacts_nested_secrets_urls_and_image_blobs(tmp_path):
    from spritewarden.events import EventStore
    store = EventStore(tmp_path / 'events.db')
    store.append('attempt.started', run_id='r', payload={
        'api_key': 'private-one', 'token': 'private-token', 'nested': [{'Authorization': 'Bearer private-two'}],
        'source_url': 'https://user:pass@example.com/img.png?X-Amz-Signature=private-three#token',
        'prompt': 'Authorization: Bearer private-four https://example.com/i?token=private-five',
        'image': 'data:image/png;base64,VERY_PRIVATE_PIXELS',
        'provider_asset_id': 'keep-this',
    })
    raw = json.dumps(store.list_events())
    for secret in ['private-one', 'private-token', 'private-two', 'private-three', 'private-four', 'private-five', 'VERY_PRIVATE_PIXELS', 'user:pass']:
        assert secret not in raw
    assert 'keep-this' in raw


def test_emit_projects_judgments_and_preserves_unavailable(tmp_path, monkeypatch):
    from spritewarden.events import emit, EventStore
    monkeypatch.setenv('SPRITEWARDEN_EVENTS_DB', str(tmp_path / 'events.db'))
    monkeypatch.setenv('SPRITEWARDEN_LEARNING_DB', str(tmp_path / 'learning.db'))
    eid = emit('judgment.completed', run_id='run-1', payload={
        'image': 'bot.png', 'provider': 'example', 'stage': 'views',
        'gates': {'vision_reference': {'passed': False, 'status': 'unavailable', 'summary': 'network unavailable'}}})
    assert eid
    assert EventStore(tmp_path / 'events.db').list_events()[0]['run_id'] == 'run-1'
    with sqlite3.connect(tmp_path / 'learning.db') as conn:
        outcome, params = conn.execute('SELECT outcome, params_json FROM episodes').fetchone()
    assert outcome == 'pending'
    assert json.loads(params)['event_id'] == eid


def test_failed_capture_warns_spools_and_replay_is_idempotent(tmp_path, monkeypatch):
    from spritewarden import events
    monkeypatch.setenv('SPRITEWARDEN_EVENTS_DB', str(tmp_path / 'events.db'))
    monkeypatch.setenv('SPRITEWARDEN_LEARNING_DB', str(tmp_path / 'learning.db'))
    original = events.EventStore.append
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError('disk full')
    with monkeypatch.context() as m:
        m.setattr(events.EventStore, 'append', fail)
        with pytest.warns(RuntimeWarning, match='capture degraded'):
            assert events.emit('attempt.started', run_id='r', payload={'secret': 'private'}) is None
    spool = tmp_path / 'spool'
    assert len(list(spool.glob('*.json'))) == 1
    assert 'private' not in next(spool.glob('*.json')).read_text()
    assert events.replay_spool()['replayed'] == 1
    assert events.replay_spool()['replayed'] == 0
    assert len(events.EventStore(tmp_path / 'events.db').list_events()) == 1


@pytest.mark.parametrize('field,value', [
    ('payload', {'decision': 'accepted', 'sha256': 'a'}),
    ('run_id', 'other-run'), ('event_type', 'attempt.completed'),
    ('created_at', '2026-01-02T00:00:00+00:00'),
])
def test_conflicting_spool_receipt_is_retained_not_projected(tmp_path, field, value):
    from spritewarden import events
    store = events.EventStore(events.default_path(tmp_path))
    event = {'event_id': 'duplicate', 'run_id': 'run', 'event_type': 'human.decision',
             'created_at': '2026-01-01T00:00:00+00:00', 'payload': {'decision': 'rejected', 'sha256': 'a'}}
    store.append(**event)
    conflicting = dict(event, **{field: value})
    receipt = events._spool(conflicting, tmp_path)
    report = events.replay_spool(tmp_path)
    assert report['replayed'] == 0
    assert len(report['failed']) == 1
    assert receipt.exists()
    assert store.list_events()[0]['payload']['decision'] == 'rejected'
    learning = tmp_path / 'learning.db'
    if learning.exists():
        with sqlite3.connect(learning) as conn:
            assert conn.execute('SELECT count(*) FROM episodes').fetchone()[0] == 0


def test_exact_duplicate_spool_receipt_projects_once(tmp_path):
    from spritewarden import events
    store = events.EventStore(events.default_path(tmp_path))
    event = {'event_id': 'duplicate', 'run_id': 'run', 'event_type': 'human.decision',
             'created_at': '2026-01-01T00:00:00+00:00', 'payload': {'sha256': 'a', 'decision': 'rejected'}}
    store.append(**event)
    for _ in range(2):
        events._spool(event, tmp_path)
        assert events.replay_spool(tmp_path)['replayed'] == 1
    assert len(store.list_events()) == 1
    with sqlite3.connect(tmp_path / 'learning.db') as conn:
        assert conn.execute('SELECT count(*) FROM episodes').fetchone()[0] == 1


def test_reconcile_scans_past_first_page_without_changing_ledger(tmp_path):
    from spritewarden import events
    store = events.EventStore(events.default_path(tmp_path))
    with store._connect() as conn:
        conn.executemany('INSERT INTO events(event_id,run_id,event_type,created_at,payload) VALUES(?,?,?,?,?)',
                         [(str(i), 'r', 'operation.started', '2026-01-01T00:00:00+00:00', '{}') for i in range(1000)])
    store.append('human.decision', payload={'decision': 'accepted'})
    before = store.path.read_bytes()
    assert events.reconcile(tmp_path) == {'scanned': 1001, 'projected': 1, 'failed': []}
    assert store.path.read_bytes() == before
    assert events.reconcile(tmp_path)['projected'] == 0


def test_concurrent_writers_keep_all_events(tmp_path):
    from spritewarden.events import EventStore
    store = EventStore(tmp_path / 'events.db')
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i: store.append('attempt.completed', run_id='r', payload={'n': i}), range(20)))
    rows = store.list_events()
    assert len(rows) == 20
    assert len({r['event_id'] for r in rows}) == 20


def test_read_only_store_never_creates_or_migrates_database(tmp_path):
    from spritewarden.events import EventStore
    missing = tmp_path / 'missing.db'
    with pytest.raises(FileNotFoundError):
        EventStore(missing, read_only=True)
    assert not missing.exists()
    store = EventStore(tmp_path / 'events.db')
    store.append('attempt.started', run_id='r')
    before = store.path.read_bytes()
    reader = EventStore(store.path, read_only=True)
    assert len(reader.list_events()) == 1
    assert store.path.read_bytes() == before
    with pytest.raises(sqlite3.OperationalError):
        reader.append('attempt.completed', run_id='r')


def test_operation_correlates_events_and_records_exceptions(tmp_path):
    from spritewarden import events
    with pytest.raises(ValueError, match='bad input'):
        with events.operation('judge', project_root=tmp_path, run_id='explicit-run') as run_id:
            events.emit('judgment.completed', payload={'gates': {}})
            raise ValueError('bad input')
    rows = events.EventStore(events.default_path(tmp_path)).list_events(run_id=run_id)
    assert [r['event_type'] for r in rows] == ['operation.started', 'judgment.completed', 'operation.failed']
    assert rows[-1]['payload']['error_type'] == 'ValueError'


def test_only_human_decisions_imply_artistic_acceptance(tmp_path):
    from spritewarden import events
    events.emit('judgment.completed', payload={'gates': {'still': {'passed': True}}})
    events.emit('human.decision', payload={'decision': 'accepted', 'sha256': 'abc', 'actor': 'owner'})
    with sqlite3.connect(tmp_path / 'learning.db') as conn:
        assert [r[0] for r in conn.execute('SELECT outcome FROM episodes ORDER BY id')] == ['pending', 'accepted']
