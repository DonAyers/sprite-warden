"""One offline-plan / explicit-execute service used by CLI and MCP.

Plans are evidence-bound, single-execution intents. Provider success is not art
approval. An interrupted execution remains claimed until explicitly reconciled;
it is never automatically retried.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from . import events


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def _file_hash(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def _within(root, path):
    root = Path(root).resolve()
    path = Path(path)
    resolved = (path if path.is_absolute() else root / path).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError('workflow paths must remain inside project_root')
    return resolved


# Include every local source alias accepted by generator.contracts, plus the
# workflow's nested dependency keys. Keep canonicalization and hashing identical.
_LOCAL_INPUT_KEYS = {'input_image', 'inputs', 'reference_image', 'source', 'source_path',
                     'anchor', 'path', 'file', 'input', 'start_image', 'source_image', 'image'}


def _canonical_inputs(root, value, key=''):
    if isinstance(value, dict):
        return {k: _canonical_inputs(root, v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_inputs(root, v, key) for v in value]
    if isinstance(value, str) and key in _LOCAL_INPUT_KEYS and not value.startswith(('https://', 'http://', 'data:')):
        return str(_within(root, value))
    return value


def _dependencies(root, params, character_dir=None):
    files = set()
    def visit(value, key=''):
        if isinstance(value, dict):
            for k, v in value.items():
                visit(v, str(k))
        elif isinstance(value, list):
            for v in value:
                visit(v, key)
        elif isinstance(value, str) and key in _LOCAL_INPUT_KEYS:
            if not value.startswith(('https://', 'http://', 'data:')):
                path = _within(root, value)
                if not path.is_file():
                    raise ValueError(f'input dependency not found: {path}')
                files.add(path)
                for parent in [path.parent, *path.parents]:
                    if not parent.is_relative_to(root):
                        break
                    files.add(_within(root, parent / 'pipeline.json'))
                    if (parent / 'pipeline.json').is_file():
                        break
    visit(params)
    if character_dir is not None:
        folder = _within(root, character_dir)
        for name in ('pipeline.json', 'workflow.json'):
            files.add(_within(root, folder / name))
    files.add(_within(root, root / 'spritewarden.json'))
    return {str(p): _file_hash(p) if p.is_file() else None for p in sorted(files)}


def plan(project_root, provider, operation, prompt, params, character_dir=None):
    """Compile one exact provider operation without contacting any provider."""
    from .router.recipes import load_registry, default_registry_path
    from .router.playbook import GenerationRecipe, sanitize_prompt_for_provider
    root = Path(project_root).resolve()
    if not root.is_dir():
        raise ValueError('project_root must be an existing directory')
    if not isinstance(params, dict):
        raise ValueError('params must be an object')
    # Plans are portable JSON, so credentials must be supplied via environment,
    # not embedded in artifacts. Image bodies are also not plan parameters.
    # Use the ledger's bounded structural traversal, rather than maintaining a
    # second parser that can disagree about embedded or multiply encoded JSON.
    events.sanitize(params, reject_sensitive=True)
    events.sanitize(prompt, reject_sensitive=True)
    from .generator.registry import capability_status
    capability = capability_status(provider, operation)
    reg = load_registry()
    entry = reg.get(provider, operation)
    if entry is None:
        raise ValueError(f'Unknown provider/operation: {provider}/{operation}')
    selected = _canonical_inputs(root, params)
    if character_dir:
        folder = _within(root, character_dir)
        pipeline = folder / 'pipeline.json'
        if pipeline.is_file():
            state = json.loads(pipeline.read_text(encoding='utf-8'))
            stage = entry.enforce.get('stage')
            chosen = state.get(f'{stage}_provider')
            if chosen and reg.canonical_provider(chosen) != entry.provider:
                raise ValueError('requested provider conflicts with recorded pipeline route')
    size = int(selected.get('size', 64))
    if size < 1 or size > 4096:
        raise ValueError('size outside supported planning range')
    recipe = GenerationRecipe(provider=entry.provider, operation=entry.operation,
        prompt=sanitize_prompt_for_provider(prompt, entry.provider, entry.operation), size=size,
        canvas_size=int(selected.get('canvas_size', size)), perspective=selected.get('perspective', 'low top-down'),
        palette_id=selected.get('palette_id', 'luap-40'), params=selected,
        rules=entry.rules_text(), suggested_gates=entry.required_gates_after,
        required_gates_before=entry.required_gates_before)
    result = {'version': 1, 'plan_id': str(uuid4()), 'project_root': str(root),
              'character_dir': str(_within(root, character_dir)) if character_dir else None,
              'recipe': recipe.model_dump(), 'registry_hash': _file_hash(default_registry_path()),
              'dependencies': _dependencies(root, selected, character_dir),
              'capability': capability,
              'approval': 'requires_explicit_spend' if capability['executable'] else 'not_executable',
              'sources': entry.sources}
    result['fingerprint'] = _hash(result)
    if not events.emit('plan.created', project_root=root, run_id=result['plan_id'],
                       payload={'fingerprint': result['fingerprint'], 'recipe': result['recipe'],
                                'registry_hash': result['registry_hash'], 'dependencies': result['dependencies'],
                                'capability': capability}):
        raise RuntimeError('cannot record plan: capture degraded')
    return result


def execute(project_root, plan, output_dir, authorize_spend=False):
    """Execute exactly once, after checking plan integrity and all dependencies."""
    from .router.recipes import default_registry_path
    from .router.playbook import GenerationRecipe
    from .generator.registry import dispatch, capability_status
    root = Path(project_root).resolve()
    original = dict(plan)
    fingerprint = original.pop('fingerprint', None)
    if not fingerprint or _hash(original) != fingerprint or original.get('project_root') != str(root):
        raise ValueError('plan changed: fingerprint/project mismatch')
    if original.get('registry_hash') != _file_hash(default_registry_path()):
        raise ValueError('registry dependency changed; create a new plan')
    for filename, expected in original.get('dependencies', {}).items():
        path = _within(root, filename)
        if expected is None:
            changed = path.exists() or path.is_symlink()
        else:
            changed = not path.is_file() or _file_hash(path) != expected
        if changed:
            raise ValueError(f'input dependency changed: {filename}')
    capability = capability_status(original['recipe']['provider'], original['recipe']['operation'])
    if not capability['executable']:
        return {'status': 'not_executable', 'plan_id': original['plan_id'], 'capability': capability}
    if not authorize_spend:
        return {'status': 'authorization_required', 'plan_id': original['plan_id']}
    store = events.EventStore(events.default_path(root))
    rows = store.list_events(run_id=original['plan_id'], limit=1000)
    if not any(row['event_type'] == 'plan.created' and row['payload'].get('fingerprint') == fingerprint for row in rows):
        raise ValueError('plan has no recorded creation receipt')
    output = _within(root, output_dir) / original['plan_id']
    # A durable claim makes concurrent/repeated invocation fail safely. A crash
    # after submission leaves unknown completion, not permission to resubmit.
    with store._connect() as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS executions(plan_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, status TEXT NOT NULL, result TEXT)')
        try:
            conn.execute('INSERT INTO executions VALUES(?,?,?,NULL)', (original['plan_id'], fingerprint, 'unknown_completion'))
        except sqlite3.IntegrityError:
            row = conn.execute('SELECT status,result FROM executions WHERE plan_id=?', (original['plan_id'],)).fetchone()
            return {'status': 'already_executed', 'execution_status': row['status'],
                    'plan_id': original['plan_id'], 'result': json.loads(row['result']) if row['result'] else None}
    if not events.emit('workflow.started', project_root=root, run_id=original['plan_id'], payload={'fingerprint': fingerprint}):
        return {'status': 'capture_degraded', 'plan_id': original['plan_id']}
    output.mkdir(parents=True, exist_ok=False)
    try:
        with events.operation('execute', project_root=root, run_id=original['plan_id']):
            result = dispatch(GenerationRecipe(**original['recipe']), output, project_root=root)
        response = {'status': 'completed' if result.success else 'failed', 'plan_id': original['plan_id'],
                    'result': result.model_dump(), 'art_approval': 'pending'}
    except Exception as exc:
        # Do not claim an exception proves the provider did not spend.
        response = {'status': 'unknown_completion', 'plan_id': original['plan_id'],
                    'error': type(exc).__name__, 'art_approval': 'pending'}
    with store._connect() as conn:
        conn.execute('UPDATE executions SET status=?,result=? WHERE plan_id=?',
                     (response['status'], json.dumps(events.sanitize(response)), original['plan_id']))
    events.emit('workflow.' + response['status'], project_root=root, run_id=original['plan_id'], payload=response)
    return response


def history(project_root, run_id=None, limit=100):
    path = events.default_path(project_root)
    if not path.exists():
        return []
    return events.EventStore(path, read_only=True).list_events(run_id=run_id, limit=limit)


def inspect(project_root, character_dir):
    from .workflow_state import inspect_state
    return inspect_state(project_root, character_dir)
