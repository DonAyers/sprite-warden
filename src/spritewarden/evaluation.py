"""Evidence-based workflow metrics: no provider rankings from missing denominators."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import math


def classify_review(case):
    """Keep observation, unavailable checks, supersession, and decisions separate."""
    if case.get('stale'):
        return 'stale'
    if case.get('claim_status') in ('retracted', 'disputed', 'hypothesized'):
        return 'needs_review'
    decision = case.get('decision')
    if decision == 'rejected':
        return 'rejected'
    checks = list((case.get('gates') or {}).values())
    if any(isinstance(g, dict) and g.get('status') == 'unavailable' for g in checks):
        return 'needs_evidence'
    if decision == 'accepted_with_exception' and case.get('exception_scoped'):
        return 'accepted_with_exception'
    failed = any(not (g if isinstance(g, bool) else g.get('passed', False)) for g in checks)
    if failed:
        return 'blocked'
    if decision == 'accepted':
        return 'accepted'
    return 'needs_review'


def evaluate_cases(cases):
    mismatches = []
    for case in cases:
        actual = classify_review(case)
        if actual != case['expected']:
            mismatches.append({'case': case['name'], 'expected': case['expected'], 'actual': actual})
    return {'passed': not mismatches, 'cases': len(cases), 'mismatches': mismatches}


def _event_order(event):
    """Replay preserves occurrence time; ledger sequence is ingestion order only."""
    try:
        stamp = datetime.fromisoformat(event['created_at'].replace('Z', '+00:00'))
        stamp = stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp
        stamp = stamp.astimezone(timezone.utc)
    except (ValueError, TypeError, KeyError):
        stamp = datetime.min.replace(tzinfo=timezone.utc)
    # Stable tie-break, never append order. Malformed timestamps cannot supersede
    # a dated decision. Exact concurrent conflicts still require human review.
    return stamp, event['event_id']


def summarize(store):
    """Read all pages and report known costs plus review disagreement candidates.

    Disagreement is a triage signal, NOT a confirmed false-positive diagnosis.
    Unknown costs stay unknown, and unavailable checks are not visual failures.
    """
    rows = []
    offset = 0
    while True:
        batch = store.list_events(limit=1000, offset=offset)
        rows.extend(batch)
        if len(batch) < 1000:
            break
        offset += len(batch)
    judgments, decisions, costs = {}, {}, {}
    accepted_runs = {}
    decision_times, decision_choices = {}, {}
    started, finished = Counter(), Counter()
    identified_starts = set()
    unavailable = 0
    for event in sorted(rows, key=_event_order):
        p = event['payload']
        revision = p.get('sha256') or p.get('revision_id')
        if event['event_type'] == 'judgment.completed':
            values = list((p.get('gates') or {}).values())
            unavailable += sum(isinstance(g, dict) and g.get('status') == 'unavailable' for g in values)
            if revision:
                available = [g for g in values if not isinstance(g, dict) or g.get('status') not in ('unavailable', 'not_applicable')]
                judgments[revision] = all(g if isinstance(g, bool) else g.get('passed', False) for g in available) if available else None
        elif event['event_type'] in ('human.decision', 'decision.recorded') and revision:
            stamp = _event_order(event)[0]
            if decision_times.get(revision) != stamp:
                decision_times[revision] = stamp
                decision_choices[revision] = set()
            decision_choices[revision].add(p.get('decision'))
            decisions[revision] = p.get('decision') if len(decision_choices[revision]) == 1 else None
            accepted_runs[revision] = event['run_id']
        elif event['event_type'] == 'attempt.started':
            if p.get('attempt_id'):
                identified_starts.add((event['run_id'], p['attempt_id']))
            else:
                started[event['run_id']] += 1
        elif event['event_type'] in ('attempt.completed', 'attempt.failed'):
            # Legacy terminal receipts without attempt IDs are distinct attempts,
            # not updates to a run's single cost. Explicit IDs deduplicate receipts.
            key = (event['run_id'], p.get('attempt_id') or event['event_id'])
            if not p.get('attempt_id'):
                finished[event['run_id']] += 1
            cost = p.get('cost_usd')
            valid = isinstance(cost, (int, float)) and not isinstance(cost, bool) and math.isfinite(cost) and cost >= 0
            cost = cost if valid else None
            if key in costs and costs[key] != cost:
                costs[key] = None  # conflicting/partial receipts are not full coverage
            else:
                costs[key] = cost
    conflicts = sorted(rev for rev, choices in decision_choices.items() if len(choices) > 1)
    accepted = {rev for rev, decision in decisions.items() if decision in ('accepted', 'accepted_with_exception')}
    false_pos = sorted(rev for rev in accepted if judgments.get(rev) is False)
    false_neg = sorted(rev for rev, decision in decisions.items() if decision == 'rejected' and judgments.get(rev) is True)
    unknown = sum(cost is None for cost in costs.values())
    unknown += len(identified_starts - costs.keys())
    unknown += sum(max(0, count - finished[run]) for run, count in started.items())
    known_runs = {run for (run, _), cost in costs.items() if cost is not None}
    known = round(sum(cost for cost in costs.values() if cost is not None), 6)
    fully_costed = bool(accepted) and not conflicts and not unknown and all(accepted_runs[r] in known_runs for r in accepted)
    return {'events': len(rows), 'human_accepted_revisions': len(accepted), 'human_rejected_revisions': sum(d == 'rejected' for d in decisions.values()),
            'unresolved_decision_conflicts': conflicts,
            'unavailable_checks': unavailable, 'false_positive_candidates': false_pos, 'false_negative_candidates': false_neg,
            'known_cost_usd': known, 'runs_with_known_cost': len(known_runs),
            'attempts_with_unknown_cost': unknown,
            'cost_per_accepted_asset_usd': round(known / len(accepted), 6) if fully_costed else None,
            'notice': 'Disagreements require review; incomplete costs and missing attempts do not establish provider success rates.'}
