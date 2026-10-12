import json


def test_cli_plan_output_is_root_relative_and_confined(tmp_path, monkeypatch, capsys):
    from spritewarden.workflow_cli import main
    root = tmp_path / 'project'
    root.mkdir()
    monkeypatch.chdir(tmp_path)
    args = ['--project', str(root), 'plan', '--provider', 'spritefusion', '--operation', 'generate']
    assert main(args + ['--out', '../escape.json']) == 2
    assert not (tmp_path / 'escape.json').exists()
    assert main(args + ['--out', 'plan.json']) == 0
    assert (root / 'plan.json').is_file()
    assert not (tmp_path / 'plan.json').exists()


def test_cli_builds_plan_and_reads_history(tmp_path, capsys):
    from spritewarden.workflow_cli import main
    path = tmp_path / 'plan.json'
    assert main(['--project', str(tmp_path), 'plan', '--provider', 'spritefusion', '--operation', 'generate',
                 '--prompt', 'a robot', '--out', str(path)]) == 0
    assert json.loads(path.read_text())['recipe']['operation'] == 'generate'
    capsys.readouterr()
    assert main(['--project', str(tmp_path), 'history']) == 0
    assert json.loads(capsys.readouterr().out)[0]['event_type'] == 'plan.created'


def test_historical_import_is_dry_by_default_and_idempotent(tmp_path, capsys):
    from spritewarden.workflow_cli import main
    from spritewarden.events import EventStore, default_path
    log = tmp_path / 'rejections.jsonl'
    log.write_text(json.dumps({'provider': 'old-choice', 'image': 'bot.png', 'failed': [{'name': 'vision_reference', 'errors': ['WinError 10013']}]}))
    args = ['--project', str(tmp_path), 'backfill', '--rejections', str(log)]
    assert main(args) == 0
    assert not default_path(tmp_path).exists()
    assert main(args + ['--apply']) == 0
    assert main(args + ['--apply']) == 0
    rows = EventStore(default_path(tmp_path)).list_events()
    assert len(rows) == 1
    assert rows[0]['payload']['provider'] == 'unknown'
    assert rows[0]['payload']['reported_provider'] == 'old-choice'
    assert rows[0]['payload']['classification'] == 'infrastructure_failure'


def test_reconcile_cli_recovers_committed_unprojected_events(tmp_path, capsys):
    import sqlite3
    from spritewarden.workflow_cli import main
    from spritewarden.events import EventStore, default_path
    args = ['--project', str(tmp_path), 'reconcile']
    assert main(args) == 0
    assert not default_path(tmp_path).exists()
    capsys.readouterr()
    store = EventStore(default_path(tmp_path))
    store.append('human.decision', run_id='run', payload={'decision': 'accepted', 'sha256': 'a'})
    assert main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report == {'scanned': 1, 'projected': 1, 'failed': []}
    with sqlite3.connect(tmp_path / 'learning.db') as conn:
        assert conn.execute('SELECT outcome FROM episodes').fetchall() == [('accepted',)]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)['projected'] == 0
    assert len(store.list_events()) == 1


def test_reconcile_cli_reports_projection_failure_then_recovers(tmp_path, capsys, monkeypatch):
    from spritewarden import events
    from spritewarden.workflow_cli import main
    store = events.EventStore(events.default_path(tmp_path))
    store.append('human.decision', event_id='pending', payload={'decision': 'rejected'})
    args = ['--project', str(tmp_path), 'reconcile']
    def fail(event):
        raise OSError('unavailable')
    with monkeypatch.context() as m:
        m.setattr(events, '_project_episode', fail)
        assert main(args) == 1
    assert json.loads(capsys.readouterr().out)['failed'] == [{'event_id': 'pending', 'error': 'OSError'}]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)['projected'] == 1


def test_metrics_does_not_create_missing_store(tmp_path, capsys):
    from spritewarden.workflow_cli import main
    from spritewarden.events import default_path
    assert main(['--project', str(tmp_path), 'metrics']) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'no_events'
    assert not default_path(tmp_path).exists()
