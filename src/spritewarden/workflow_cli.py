"""JSON-first workflow CLI; execution never spends without an explicit flag."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3

from . import events, workflow


def backfill(project_root, path, apply=False):
    """Import historical observations without inventing missing attempts/outcomes."""
    source = Path(path).resolve()
    records = []
    for number, line in enumerate(source.read_text(encoding='utf-8-sig').splitlines(), 1):
        if not line.strip():
            continue
        record = json.loads(line)
        if not isinstance(record, dict):
            raise ValueError(f'expected object on line {number}')
        text = json.dumps(record).lower()
        infrastructure = any(term in text for term in ('winerror 10013', 'socket', 'network unavailable', 'connection refused'))
        payload = {'historical': True, 'source_file': str(source), 'source_line': number,
                   'provider': 'unknown', 'reported_provider': record.get('provider'),
                   'classification': 'infrastructure_failure' if infrastructure else 'historical_observation',
                   'observation': record, 'missing': ['request_payload', 'complete_attempt_history', 'verified_provider']}
        eid = 'history-' + hashlib.sha256((str(source) + '\n' + str(number) + '\n' + line).encode()).hexdigest()
        records.append({'event_id': eid, 'payload': payload})
    imported = 0
    if apply:
        store = events.EventStore(events.default_path(project_root))
        for entry in records:
            try:
                store.append('history.imported', run_id=entry['event_id'], event_id=entry['event_id'], payload=entry['payload'])
                imported += 1
            except sqlite3.IntegrityError:
                with store._connect() as conn:
                    if not conn.execute('SELECT 1 FROM events WHERE event_id=?', (entry['event_id'],)).fetchone():
                        raise
    return {'dry_run': not apply, 'observations': len(records), 'imported': imported,
            'notice': 'Historical observations do not establish provider success rates or human approval.'}


def parser():
    p = argparse.ArgumentParser(prog='warden workflow')
    p.add_argument('--project', default='.', help='Project root; paths are scoped to this directory')
    sub = p.add_subparsers(dest='command', required=True)
    pp = sub.add_parser('plan', help='Compile a source-bound plan without network calls')
    pp.add_argument('--provider', required=True)
    pp.add_argument('--operation', required=True)
    pp.add_argument('--prompt', default='')
    pp.add_argument('--params', default='{}', help='JSON object; credentials and image bodies forbidden')
    pp.add_argument('--character')
    pp.add_argument('--out', help='New JSON file; existing files are never overwritten')
    pe = sub.add_parser('execute', help='Execute a recorded plan once; defaults to no spend')
    pe.add_argument('plan_file')
    pe.add_argument('--out', required=True)
    pe.add_argument('--authorize-spend', action='store_true')
    ph = sub.add_parser('history', help='Read the append-only evidence ledger')
    ph.add_argument('--run-id')
    ph.add_argument('--limit', type=int, default=100)
    sub.add_parser('metrics', help='Read evidence coverage, known cost and review disagreement candidates')
    sub.add_parser('replay-spool', help='Replay sanitized receipts; never resubmits provider jobs')
    sub.add_parser('reconcile', help='Repair missing learning projections from committed ledger events; no provider calls')
    pb = sub.add_parser('backfill', help='Preview import of historical rejection observations')
    pb.add_argument('--rejections', required=True)
    pb.add_argument('--apply', action='store_true')
    pi = sub.add_parser('inspect', help='Inspect current state and immutable revision history')
    pi.add_argument('character')
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == 'plan':
            output = workflow._within(args.project, args.out) if args.out else None
            result = workflow.plan(args.project, args.provider, args.operation, args.prompt, json.loads(args.params), args.character)
            if output:
                with output.open('x', encoding='utf-8') as stream:
                    json.dump(result, stream, indent=2)
        elif args.command == 'execute':
            result = workflow.execute(args.project, json.loads(Path(args.plan_file).read_text(encoding='utf-8')),
                                      args.out, authorize_spend=args.authorize_spend)
        elif args.command == 'history':
            result = workflow.history(args.project, args.run_id, args.limit)
        elif args.command == 'metrics':
            from .evaluation import summarize
            path = events.default_path(args.project)
            result = summarize(events.EventStore(path, read_only=True)) if path.exists() else {'status': 'no_events', 'events': 0}
        elif args.command == 'reconcile':
            result = events.reconcile(args.project)
        elif args.command == 'replay-spool':
            result = events.replay_spool(args.project)
        elif args.command == 'backfill':
            result = backfill(args.project, args.rejections, args.apply)
        else:
            result = workflow.inspect(args.project, args.character)
        print(json.dumps(result, indent=2, default=str))
        if isinstance(result, dict) and (result.get('status') in ('failed', 'unknown_completion', 'capture_degraded')
                                         or (args.command in ('reconcile', 'replay-spool') and result.get('failed'))):
            return 1
        return 0
    except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
        print(json.dumps({'error': str(exc), 'error_type': type(exc).__name__}))
        return 2
