"""A small stdlib-only append-only event ledger shared by every interface.

The ledger stores evidence, not approval. Payloads are sanitized before any
persistence. Never put credentials or raw image bodies in workflow events.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4
from contextlib import contextmanager
from contextvars import ContextVar

_RUN_ID = ContextVar('warden_run_id', default=None)
_PROJECT = ContextVar('warden_project', default=None)


def current_run_id():
    return _RUN_ID.get()


@contextmanager
def operation(name, *, project_root=None, payload=None, run_id=None):
    """Correlate nested checks, and persist exceptions without hiding them."""
    run_id = run_id or current_run_id() or str(uuid4())
    run_token = _RUN_ID.set(run_id)
    project_token = _PROJECT.set(project_root or _PROJECT.get())
    try:
        if not emit('operation.started', payload={'operation': name, **(payload or {})}):
            raise RuntimeError('cannot record operation intent: capture degraded')
        try:
            yield run_id
        except BaseException as exc:
            emit('operation.failed', payload={'operation': name, 'error_type': type(exc).__name__})
            raise
        else:
            emit('operation.completed', payload={'operation': name})
    finally:
        _RUN_ID.reset(run_token)
        _PROJECT.reset(project_token)

_SECRET = re.compile(r'(authorization|api[_-]?key|password|passwd|secret|token|credential|cookie|signature|base64|data_url)', re.I)
_URL = re.compile(r'https?://[^\s<>\"\']+', re.I)
_BEARER = re.compile(r'(?i)\bbearer\s+[A-Za-z0-9_.~+/=-]+')
_INLINE_SECRET = re.compile(
    r'''\b([\w-]+)\s*[:=]\s*(?:"(?:\\.|[^"\\])*(?:"|$)|'(?:\\.|[^'\\])*(?:'|$)|[^\s,;]+)''',
    re.S,
)


def _unique_json_object(pairs):
    """Duplicate keys are ambiguous: never discard an earlier secret value."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('ambiguous JSON object')
        result[key] = value
    return result


_JSON_DECODER = json.JSONDecoder(object_pairs_hook=_unique_json_object)


def _safe_url(match):
    try:
        p = urlsplit(match.group(0))
        host = p.hostname or ''
        if ':' in host:
            host = '[' + host + ']'
        if p.port:
            host += ':' + str(p.port)
        return urlunsplit((p.scheme, host, p.path, '', ''))
    except ValueError:
        return '[redacted-url]'


class _SanitizeLimit(Exception):
    """Internal signal; never include untrusted input in error messages."""


class _Sanitizer:
    """One shared budget across containers, fragments and serialized layers.

    Bound text before decoding (20k per string, 200k total), successful decodes
    (256), visited values (10k), and recursion (32). A failed decode is terminal,
    not a retry at every following character. No partially inspected payload is
    returned when a budget is exhausted.
    """
    def __init__(self):
        self.nodes = 10000
        self.characters = 200000
        self.fragments = 256
        self.redactions = 0

    def redact(self):
        self.redactions += 1
        return '[redacted]'

    def charge_text(self, value):
        self.characters -= len(value)
        if len(value) > 20000 or self.characters < 0:
            raise _SanitizeLimit

    def visit(self, value, key='', depth=0):
        self.nodes -= 1
        if self.nodes < 0 or depth > 32:
            raise _SanitizeLimit
        self.charge_text(key)
        if _SECRET.search(key):
            return self.redact()
        if isinstance(value, dict):
            return {str(k): self.visit(v, str(k), depth + 1) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.visit(v, depth=depth + 1) for v in value]
        if isinstance(value, bytes):
            self.redactions += 1
            return {'sha256': hashlib.sha256(value).hexdigest(), 'bytes': len(value)}
        if isinstance(value, str):
            return self.text(value, depth)
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return self.visit(str(value), depth=depth + 1)

    def text(self, value, depth):
        self.charge_text(value)
        if value in ('[redacted]', '[redacted-url]', '[redacted-image]'):
            return value
        position = 0
        while position < len(value):
            if value[position] not in '{["':
                position += 1
                continue
            self.fragments -= 1
            if self.fragments < 0:
                raise _SanitizeLimit
            try:
                decoded, end = _JSON_DECODER.raw_decode(value, position)
            except (ValueError, RecursionError):
                # Truncated, malformed, duplicate-key or too-deep JSON is not
                # provably safe. Do not log decoder errors or return its suffix.
                return self.redact()
            # A truncated object may retain a valid escaped key token without
            # its opening brace. Inspect the decoded key, not its source spelling.
            if isinstance(decoded, str) and value[end:].lstrip().startswith(':') and _SECRET.search(decoded):
                return self.redact()
            before = self.redactions
            self.visit(decoded, depth=depth + 1)
            if self.redactions != before:
                return self.redact()
            # raw_decode always consumes input; strings lose their outer quotes.
            # Only objects/arrays/strings are scanned, so scalar text terminates.
            position = end
        # Compatibility for quoted keys in non-JSON/Python-style diagnostics.
        if any(_SECRET.search(m.group(2)) for m in re.finditer(r'''(["'])([^"']+)\1\s*:''', value)):
            return self.redact()
        cleaned = re.sub(r'data:image/[^\s]+', '[redacted-image]', value, flags=re.I)
        cleaned = _URL.sub(_safe_url, cleaned)
        cleaned = _BEARER.sub('Bearer [redacted]', cleaned)
        cleaned = _INLINE_SECRET.sub(
            lambda m: m.group(1) + '=[redacted]' if _SECRET.search(m.group(1)) else m.group(0), cleaned)
        if cleaned != value:
            self.redactions += 1
        return cleaned


def sanitize(value, key='', *, reject_sensitive=False):
    """Redact untrusted evidence, or reject unsafe plans using the same traversal.

    Safe text is preserved byte-for-byte. Text containing unsafe structured
    fragments is suppressed as a whole; malformed fragments fail conservatively.
    Strict mode also rejects sensitive keys whose value is already '[redacted]'.
    """
    sanitizer = _Sanitizer()
    try:
        result = sanitizer.visit(value, key)
    except (_SanitizeLimit, RecursionError):
        result = sanitizer.redact()
    if reject_sensitive and sanitizer.redactions:
        raise ValueError('credentials and inline image bodies are not allowed in plans; input must be safely inspectable')
    return result


def default_path(project_root=None):
    """An override wins; otherwise keep evidence with the explicit project."""
    if os.environ.get('SPRITEWARDEN_EVENTS_DB'):
        return Path(os.environ['SPRITEWARDEN_EVENTS_DB']).resolve()
    root = Path(project_root or Path.cwd()).resolve()
    return root / '.spritewarden' / 'events.db'


class EventStore:
    def __init__(self, path, read_only=False):
        self.path = Path(path)
        self.read_only = read_only
        if read_only:
            if not self.path.is_file():
                raise FileNotFoundError(self.path)
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    run_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_run ON events(run_id, sequence);
                CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
                  BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
                  BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
            ''')

    def _connect(self):
        # A context manager for transactions alone does not close sqlite handles.
        from contextlib import contextmanager

        @contextmanager
        def connection():
            if self.read_only:
                conn = sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
            else:
                conn = sqlite3.connect(self.path, timeout=10)
            conn.row_factory = sqlite3.Row
            try:
                with conn:
                    yield conn
            finally:
                conn.close()
        return connection()

    def append(self, event_type, *, run_id=None, payload=None, event_id=None, created_at=None):
        if not isinstance(event_type, str) or not event_type.strip():
            raise ValueError('event_type must be nonempty')
        event_id = event_id or str(uuid4())
        run_id = run_id or event_id
        body = json.dumps(sanitize(payload or {}), ensure_ascii=False, allow_nan=False)
        if len(body.encode('utf-8')) > 1000000:
            raise ValueError('event payload exceeds 1 MB')
        with self._connect() as conn:
            conn.execute('INSERT INTO events(event_id,run_id,event_type,created_at,payload) VALUES(?,?,?,?,?)',
                         (event_id, run_id, event_type, created_at or datetime.now(timezone.utc).isoformat(), body))
        return event_id

    def list_events(self, run_id=None, limit=100, offset=0):
        limit = max(1, min(int(limit), 1000))
        offset = max(0, int(offset))
        query = 'SELECT * FROM events'
        args = []
        if run_id is not None:
            query += ' WHERE run_id=?'
            args.append(run_id)
        query += ' ORDER BY sequence LIMIT ? OFFSET ?'
        args += [limit, offset]
        with self._connect() as conn:
            rows = [dict(row) for row in conn.execute(query, args)]
        for row in rows:
            row['payload'] = json.loads(row['payload'])
        return rows


def _project_episode(event):
    """Idempotently project evidence; only explicit human choices imply acceptance."""
    if event['event_type'] not in {'judgment.completed', 'human.decision', 'decision.recorded',
                                   'asset.rejected', 'attempt.completed', 'attempt.failed'}:
        return False
    from .learning.db import DEFAULT_DB_PATH, SCHEMA
    target = Path(os.environ.get('SPRITEWARDEN_LEARNING_DB') or DEFAULT_DB_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=10)
    try:
        conn.executescript(SCHEMA)
        with conn:
            conn.execute('CREATE TABLE IF NOT EXISTS event_projections(event_id TEXT PRIMARY KEY, episode_id INTEGER NOT NULL)')
            if conn.execute('SELECT 1 FROM event_projections WHERE event_id=?', (event['event_id'],)).fetchone():
                return False
            p = event['payload']
            decision = p.get('decision') or p.get('action')
            human = event['event_type'] in {'human.decision', 'decision.recorded', 'asset.rejected'}
            outcome = 'pending'
            if human:
                if decision in ('accept', 'accepted', 'accepted_with_exception'):
                    outcome = 'accepted'
                elif decision in ('reject', 'rejected') or event['event_type'] == 'asset.rejected':
                    outcome = 'rejected'
            params = dict(p.get('params') or {})
            params.update(event_id=event['event_id'], run_id=event['run_id'],
                          outcome_basis='human' if human else 'evidence_only',
                          event_type=event['event_type'])
            cur = conn.execute('''INSERT INTO episodes(ts,game,asset_type,perspective,character_id,provider,
                operation,params_json,prompt,gates_json,vision_json,outcome,reason,cost_usd,duration_ms,asset_id)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (
                event['created_at'], p.get('game') or 'global', p.get('asset_type') or 'sprite',
                p.get('perspective') or 'unknown', p.get('character_id'), p.get('provider') or 'unknown',
                p.get('operation') or p.get('stage') or 'unknown', json.dumps(params), p.get('prompt') or '',
                json.dumps(p.get('gates') or {}), json.dumps(p.get('vision') or {}), outcome,
                p.get('reason') or '', p.get('cost_usd'), p.get('duration_ms'), p.get('asset_id') or p.get('sha256')))
            conn.execute('INSERT INTO event_projections VALUES(?,?)', (event['event_id'], cur.lastrowid))
        return True
    finally:
        conn.close()


def _spool(event, project_root=None):
    folder = Path(os.environ.get('SPRITEWARDEN_EVENT_SPOOL') or default_path(project_root).parent / 'spool')
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (event['event_id'] + '.json')
    temporary = path.with_suffix('.tmp-' + str(uuid4()))
    temporary.write_text(json.dumps(event, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    temporary.replace(path)
    return path


def emit(event_type, *, project_root=None, run_id=None, payload=None):
    """Record sanitized evidence, warning and spooling on persistence failure.

    Return None if the primary ledger did not commit. Spending callers must stop
    when their initial intent cannot be committed. A post-spend failure must not
    trigger another provider request; reconcile/replay its preserved receipt.
    """
    import warnings
    event_id = str(uuid4())
    project_root = project_root or _PROJECT.get()
    event = {'event_id': event_id, 'run_id': run_id or current_run_id() or event_id, 'event_type': event_type,
             'created_at': datetime.now(timezone.utc).isoformat(), 'payload': sanitize(payload or {})}
    committed = False
    try:
        store = EventStore(default_path(project_root))
        store.append(event_type, run_id=event['run_id'], payload=event['payload'],
                     event_id=event_id, created_at=event['created_at'])
        committed = True
        _project_episode(event)
    except Exception as exc:
        try:
            _spool(event, project_root)
            detail = 'sanitized receipt spooled'
        except Exception:
            detail = 'spool also failed; receipt NOT persisted'
        warnings.warn(f'Warden capture degraded ({type(exc).__name__}); {detail}', RuntimeWarning, stacklevel=2)
    return event_id if committed else None


def reconcile(project_root=None):
    """Repair the commit/projection crash window without provider calls.

    The immutable ledger is authoritative. Projection deduplication and episode
    insertion share one transaction; retrying this full scan is safe.
    """
    report = {'scanned': 0, 'projected': 0, 'failed': []}
    path = default_path(project_root)
    if not path.is_file():
        return report
    store = EventStore(path, read_only=True)
    offset = 0
    while True:
        batch = store.list_events(limit=1000, offset=offset)
        for event in batch:
            report['scanned'] += 1
            try:
                report['projected'] += bool(_project_episode(event))
            except Exception as exc:
                report['failed'].append({'event_id': event['event_id'], 'error': type(exc).__name__})
        if len(batch) < 1000:
            break
        offset += len(batch)
    return report


def replay_spool(project_root=None):
    """Replay receipts once; leave any failed receipt intact for a later retry."""
    folder = Path(os.environ.get('SPRITEWARDEN_EVENT_SPOOL') or default_path(project_root).parent / 'spool')
    store = EventStore(default_path(project_root))
    report = {'replayed': 0, 'failed': []}
    for path in sorted(folder.glob('*.json')):
        try:
            event = json.loads(path.read_text(encoding='utf-8'))
            try:
                store.append(event['event_type'], run_id=event['run_id'], payload=event['payload'],
                             event_id=event['event_id'], created_at=event['created_at'])
            except sqlite3.IntegrityError:
                with store._connect() as conn:
                    row = conn.execute('SELECT event_id,run_id,event_type,created_at,payload FROM events WHERE event_id=?',
                                       (event['event_id'],)).fetchone()
                if row is None:
                    raise
                committed = dict(row)
                committed['payload'] = json.loads(committed['payload'])
                candidate = {key: event[key] for key in committed}
                candidate['payload'] = sanitize(candidate['payload'])
                canonical = lambda value: json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
                if canonical(committed) != canonical(candidate):
                    raise ValueError('conflicting event_id: receipt retained, not projected')
                event = committed
            event['payload'] = sanitize(event['payload'])
            _project_episode(event)
            path.unlink()
            report['replayed'] += 1
        except Exception as exc:
            report['failed'].append({'receipt': path.name, 'error': type(exc).__name__})
    return report
