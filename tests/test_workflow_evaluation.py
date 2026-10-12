import json
import pytest
from pathlib import Path


@pytest.mark.parametrize('reverse', [False, True])
def test_latest_concurrent_decisions_are_unresolved_until_newer_review(tmp_path, reverse):
    from spritewarden.events import EventStore
    from spritewarden.evaluation import summarize
    store = EventStore(tmp_path / 'metrics.db')
    store.append('attempt.completed', run_id='one', payload={'cost_usd': 0.2})
    store.append('human.decision', run_id='one', created_at='2026-01-01T00:00:00Z',
                 payload={'sha256': 'stable', 'decision': 'accepted'})
    for revision, passed in [('a', False), ('b', True)]:
        store.append('judgment.completed', payload={'sha256': revision, 'gates': {'check': passed}})
        choices = [('accepted', '2026-01-02T09:00:00Z'), ('rejected', '2026-01-02T11:00:00+02:00')]
        for i, (decision, stamp) in enumerate(reversed(choices) if reverse else choices):
            store.append('human.decision', event_id=f'{revision}-{i}', run_id='one', created_at=stamp,
                         payload={'sha256': revision, 'decision': decision})
    report = summarize(store)
    assert report['human_accepted_revisions'] == 1
    assert report['human_rejected_revisions'] == 0
    assert report['unresolved_decision_conflicts'] == ['a', 'b']
    assert report['false_positive_candidates'] == []
    assert report['false_negative_candidates'] == []
    assert report['cost_per_accepted_asset_usd'] is None
    for revision in ['a', 'b']:
        store.append('decision.recorded', run_id='one', created_at='2026-01-02T10:00:00Z',
                     payload={'sha256': revision, 'decision': 'rejected'})
    report = summarize(store)
    assert report['unresolved_decision_conflicts'] == []
    assert report['human_accepted_revisions'] == 1
    assert report['human_rejected_revisions'] == 2
    assert report['false_negative_candidates'] == ['b']
    assert report['cost_per_accepted_asset_usd'] == 0.2


def test_evaluation_separates_unavailable_and_owner_disagreement(tmp_path):
    from spritewarden.events import EventStore
    from spritewarden.evaluation import summarize
    store = EventStore(tmp_path / 'metrics.db')
    store.append('attempt.completed', run_id='one', payload={'provider': 'example', 'operation': 'animate', 'cost_usd': 0.2})
    store.append('judgment.completed', run_id='one', payload={'sha256': 'a', 'gates': {'vision_reference': {'passed': False, 'status': 'unavailable'}}})
    store.append('human.decision', run_id='one', payload={'sha256': 'a', 'decision': 'accepted'})
    store.append('judgment.completed', run_id='two', payload={'sha256': 'b', 'gates': {'symmetry': {'passed': False, 'status': 'fail'}}})
    store.append('human.decision', run_id='two', payload={'sha256': 'b', 'decision': 'accepted_with_exception'})
    report = summarize(store)
    assert report['unavailable_checks'] == 1
    assert report['false_positive_candidates'] == ['b']
    assert report['known_cost_usd'] == 0.2
    assert report['cost_per_accepted_asset_usd'] is None  # incomplete cost coverage
    assert report['human_accepted_revisions'] == 2


def test_metrics_aggregate_distinct_attempts_and_withhold_incomplete_ratio(tmp_path):
    from spritewarden.events import EventStore
    from spritewarden.evaluation import summarize
    store = EventStore(tmp_path / 'metrics.db')
    store.append('attempt.completed', run_id='one', payload={'attempt_id': 'a', 'cost_usd': 0.2})
    store.append('attempt.completed', run_id='one', payload={'attempt_id': 'b', 'cost_usd': 0.3})
    store.append('attempt.completed', run_id='one', payload={'attempt_id': 'b', 'cost_usd': 0.3})
    store.append('human.decision', run_id='one', payload={'sha256': 'a', 'decision': 'accepted'})
    report = summarize(store)
    assert report['known_cost_usd'] == 0.5
    assert report['cost_per_accepted_asset_usd'] == 0.5
    store.append('attempt.failed', run_id='other', payload={'cost_usd': None})
    report = summarize(store)
    assert report['known_cost_usd'] == 0.5
    assert report['cost_per_accepted_asset_usd'] is None
    assert report['attempts_with_unknown_cost'] == 1


def test_metrics_unfinished_attempt_is_unknown_not_free(tmp_path):
    from spritewarden.events import EventStore
    from spritewarden.evaluation import summarize
    store = EventStore(tmp_path / 'metrics.db')
    store.append('attempt.started', run_id='one')
    store.append('attempt.completed', run_id='one', payload={'cost_usd': 0.2})
    store.append('human.decision', run_id='one', payload={'sha256': 'a', 'decision': 'accepted'})
    assert summarize(store)['cost_per_accepted_asset_usd'] == 0.2
    store.append('attempt.started', run_id='one')
    assert summarize(store)['cost_per_accepted_asset_usd'] is None


def test_late_replay_cannot_override_newer_decision(tmp_path):
    from spritewarden.events import EventStore
    from spritewarden.evaluation import summarize
    store = EventStore(tmp_path / 'metrics.db')
    store.append('human.decision', event_id='new', run_id='one', created_at='2026-01-02T09:00:00+00:00',
                 payload={'sha256': 'a', 'decision': 'rejected'})
    store.append('human.decision', event_id='old', run_id='one', created_at='2026-01-02T10:00:00+02:00',
                 payload={'sha256': 'a', 'decision': 'accepted'})
    report = summarize(store)
    assert report['human_accepted_revisions'] == 0
    assert report['human_rejected_revisions'] == 1


def test_legacy_attempt_receipts_sum_within_run(tmp_path):
    from spritewarden.events import EventStore
    from spritewarden.evaluation import summarize
    store = EventStore(tmp_path / 'metrics.db')
    for cost in (0.2, 0.3):
        store.append('attempt.started', run_id='one')
        store.append('attempt.completed', run_id='one', payload={'cost_usd': cost})
    store.append('human.decision', run_id='one', payload={'sha256': 'a', 'decision': 'accepted'})
    report = summarize(store)
    assert report['known_cost_usd'] == 0.5
    assert report['runs_with_known_cost'] == 1
    assert report['cost_per_accepted_asset_usd'] == 0.5
    store.append('attempt.failed', run_id='one', payload={'cost_usd': None})
    assert summarize(store)['cost_per_accepted_asset_usd'] is None


def test_regression_corpus_preserves_scope_and_retractions():
    from spritewarden.evaluation import evaluate_cases
    path = Path(__file__).parent / 'fixtures' / 'workflow_cases.json'
    report = evaluate_cases(json.loads(path.read_text()))
    assert report['passed']
    assert report['cases'] >= 7
