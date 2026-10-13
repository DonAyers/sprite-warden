"""Revisioned, project-confined workflow state. Legacy migration is proposal-only."""
from __future__ import annotations

import json
from pathlib import Path
from typing import TypedDict, Any, Literal

Stage = Literal['anchor', 'views', 'anim']
DecisionKind = Literal['accepted', 'rejected', 'accepted_with_exception']


class ArtifactSnapshot(TypedDict):
    path: str
    sha256: str
    evidence: dict[str, Any] | None


class Revision(TypedDict):
    revision_id: str
    stage: Stage
    files: dict[str, ArtifactSnapshot]
    provider: str
    operation: str
    dependencies: dict[str, str]
    expected_facings: list[str]
    native_ids: dict[str, str]
    pipeline_path: str
    design_hash: str
    pipeline_evidence_current: bool
    run_id: str


class HumanDecision(TypedDict):
    decision_id: str
    revision_id: str
    decision: DecisionKind
    actor: str
    reason: str
    gate: str | None
    operation: str
    provider: str
    sha256: str
    artifact_hashes: dict[str, str]


class CurrentState(TypedDict):
    schema_version: int
    version: int
    current: dict[Stage, str]
    revisions: dict[str, Revision]
    decisions: list[HumanDecision]


def _folder(project_root, character_folder):
    root = Path(project_root).resolve()
    folder = Path(character_folder)
    folder = (folder if folder.is_absolute() else root / folder).resolve()
    if not folder.is_relative_to(root):
        raise ValueError('character folder outside project')
    if not folder.is_dir():
        raise ValueError('character folder must already exist')
    return root, folder


def _load(folder):
    path = folder / 'pipeline.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}


def _state(data):
    return data.get('_workflow', {'schema_version': 1, 'version': 0, 'current': {}, 'revisions': {}, 'decisions': []})


def inspect_state(project_root, character_folder):
    _, folder = _folder(project_root, character_folder)
    state = _state(_load(folder))
    state['readiness'] = {rid: _readiness(Path(project_root).resolve(), state, rev)
                          for rid, rev in state['revisions'].items()}
    return state


def source_revision_reasons(project_root, path):
    """Hard review constraints for a source associated with revisioned state."""
    from .enforce.core import find_pipeline
    root = Path(project_root).resolve()
    path = Path(path).resolve()
    data, pipeline = find_pipeline(path)
    if not data or '_workflow' not in data:
        return []
    if not pipeline.resolve().is_relative_to(root):
        return ['revision pipeline outside project']
    state = _state(data)
    matches = [rev for rev in state['revisions'].values()
               if any((root / item['path']).resolve() == path for item in rev['files'].values())]
    if not matches:
        return ['source has no registered selection in revisioned pipeline']
    from .gates.records import load_record
    digest = _hash(path)
    evidence = load_record(path)
    exact = [rev for rev in matches if any(
        (root / item['path']).resolve() == path and item['sha256'] == digest
        and item['evidence'] == evidence for item in rev['files'].values())]
    selected = [rev for rev in exact if state['current'].get(rev['stage']) == rev['revision_id']]
    if selected:
        return list(dict.fromkeys(reason for rev in selected
                                 for reason in _readiness(root, state, rev)['reasons']))
    reasons = ['source revision selection is missing or stale']
    for rev in exact:
        reasons.extend(_readiness(root, state, rev)['reasons'])
    return list(dict.fromkeys(reasons))


def propose_migration(project_root, character_folder):
    _, folder = _folder(project_root, character_folder)
    legacy = _load(folder)
    return {'requires_review': True, 'legacy': legacy, 'current': {},
            'unresolved': sorted(k for k in legacy if k != '_workflow'),
            'message': 'Select exact artifacts and native IDs explicitly; narrative is not an approval.'}


class StateConflict(ValueError):
    """A writer must refresh its state version before retrying."""


def _hash(path):
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _safe_file(root, raw):
    path = (root / raw).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError('artifact must be an existing project file')
    return path


def _mutate(project_root, character_folder, expected_version, action, event_type, run_id):
    import os
    from uuid import uuid4
    from . import events
    root, folder = _folder(project_root, character_folder)
    lock = folder / '.pipeline.lock'
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise StateConflict('pipeline busy; refresh and retry') from None
    temp = folder / ('.pipeline-' + uuid4().hex + '.tmp')
    try:
        os.close(fd)
        data = _load(folder); state = _state(data)
        if type(expected_version) is not int or state['version'] != expected_version:
            raise StateConflict('pipeline version changed')
        result, payload = action(root, state, data)
        state['version'] += 1
        data['_workflow'] = state
        payload.update(character_folder=folder.relative_to(root).as_posix(), version=state['version'])
        # Intent is durable before state changes; completion follows atomic replacement.
        if events.emit(event_type + '.intent', project_root=root, run_id=run_id, payload=payload) is None:
            raise RuntimeError('event ledger unavailable; state unchanged')
        with temp.open('w', encoding='utf-8') as stream:
            json.dump(data, stream, indent=2); stream.flush(); os.fsync(stream.fileno())
        os.replace(temp, folder / 'pipeline.json')
        events.emit(event_type, project_root=root, run_id=run_id, payload=payload)
        return result if result is not None else state
    finally:
        temp.unlink(missing_ok=True); lock.unlink(missing_ok=True)


def design_fingerprint(data):
    """Design inputs only: approval history and machine pointers are not design changes."""
    import hashlib
    # Only explicitly published pointers/history are bookkeeping. Unknown keys
    # are semantic by default, including facing, route and future design fields.
    bookkeeping = {'_workflow', 'anchor', 'views_dir', 'animation_paths', 'identity', 'anchor_asset_id'}
    return hashlib.sha256(json.dumps({k: v for k, v in data.items() if k not in bookkeeping},
                                    sort_keys=True).encode()).hexdigest()


def register_revision(project_root, character_folder, *, stage, files, provider, operation,
                      expected_version, dependencies=None, expected_facings=None, native_ids=None, run_id=None):
    """Snapshot evidence and byte fingerprints; never infer approval or publish pointers."""
    import hashlib
    from uuid import uuid4
    from .gates.records import load_record
    run_id = run_id or uuid4().hex
    if stage not in {'anchor', 'views', 'anim'} or not files or not provider or not operation:
        raise ValueError('stage, files, provider and operation required')
    if stage == 'views' and (not expected_facings or set(files) != set(expected_facings)):
        raise ValueError('views must exactly match expected facings')
    def action(root, state, data):
        deps = dict(dependencies or {})
        for role, rid in deps.items():
            if rid not in state['revisions']:
                raise ValueError('unknown dependency revision')
        if stage != 'anchor' and not deps:
            raise ValueError('descendant requires dependency revisions')
        artifacts = {}
        for facing, raw in files.items():
            path = _safe_file(root, raw)
            digest = _hash(path)
            objects = (root / '.spritewarden' / 'objects').resolve()
            if not objects.is_relative_to(root):
                raise ValueError('object store outside project')
            objects.mkdir(parents=True, exist_ok=True)
            archive = objects / digest
            if not archive.exists():
                blob = path.read_bytes()
                if hashlib.sha256(blob).hexdigest() != digest:
                    raise StateConflict('artifact changed during snapshot')
                try:
                    with archive.open('xb') as stream:
                        stream.write(blob)
                except FileExistsError:
                    pass
            if _hash(archive) != digest:
                raise ValueError('corrupt immutable artifact')
            artifacts[facing] = {'path': path.relative_to(root).as_posix(), 'sha256': digest,
                                 'evidence': load_record(path)}
        _, folder = _folder(root, character_folder)
        pipeline = folder / 'pipeline.json'
        pipeline_hash = _hash(pipeline) if pipeline.is_file() else None
        pipeline_evidence_current = all(
            not (item['evidence'] or {}).get('dependencies', {}).get('pipeline') or
            (item['evidence'] or {})['dependencies']['pipeline'].get('sha256') == pipeline_hash
            for item in artifacts.values())
        rev = {'stage': stage, 'files': artifacts, 'provider': provider, 'operation': operation,
               'pipeline_path': pipeline.relative_to(root).as_posix(), 'design_hash': design_fingerprint(data),
               'pipeline_evidence_current': pipeline_evidence_current,
               'dependencies': deps, 'expected_facings': sorted(expected_facings or files),
               'native_ids': dict(native_ids or {}), 'run_id': run_id}
        rid = hashlib.sha256(json.dumps(rev, sort_keys=True).encode()).hexdigest()
        rev['revision_id'] = rid
        state['revisions'].setdefault(rid, rev)
        return rev, {'revision': rev}
    return _mutate(project_root, character_folder, expected_version, action, 'revision.registered', run_id)


def _readiness(root, state, rev, seen=None):
    from .gates.records import stage_readiness, load_record
    reasons = []; seen = set(seen or ())
    rid = rev['revision_id']
    if rid in seen:
        return {'ready': False, 'reasons': ['cyclic dependency']}
    seen.add(rid)
    pipeline = _safe_file(root, rev['pipeline_path'])
    design_current = design_fingerprint(json.loads(pipeline.read_text(encoding='utf-8'))) == rev['design_hash']
    if not design_current:
        reasons.append('design changed')
    if rev['stage'] == 'views':
        selected = {(root / item['path']).resolve() for item in rev['files'].values()}
        parents = {p.parent for p in selected}
        if len(parents) != 1 or {p.resolve() for p in next(iter(parents)).glob('*.png')} != selected:
            reasons.append('views directory contains unselected or missing artifacts')
    decisions = [d for d in state['decisions'] if d['revision_id'] == rid]
    latest = decisions[-1] if decisions else {}
    if latest.get('decision') == 'rejected':
        reasons.append('human rejected revision')
    for role, dep in rev['dependencies'].items():
        ancestor = state['revisions'].get(dep)
        if not ancestor or state['current'].get(ancestor['stage']) != dep or not _readiness(root, state, ancestor, seen)['ready']:
            reasons.append('stale dependency: ' + role)
    for item in rev['files'].values():
        try:
            path = _safe_file(root, item['path'])
            if _hash(path) != item['sha256']:
                reasons.append('artifact bytes changed'); continue
            if load_record(path) != item['evidence']:
                reasons.append('gate evidence changed; register a new revision'); continue
            result = stage_readiness(path, {'anchor': 'start_image'}.get(rev['stage'], rev['stage']))
            remaining = list(result['reasons'])
            if design_current and rev.get('pipeline_evidence_current'):
                # The pipeline's decision log changed, not its captured design inputs.
                remaining = [r for r in remaining if r != 'dependency pipeline changed or unavailable']
            # Only named artistic measurements can be waived, never missing evidence/contracts.
            if latest.get('decision') == 'accepted_with_exception' and latest.get('operation') == rev['operation']:
                gate = latest['gate']
                remaining = [r for r in remaining if r not in {gate + ' failed or unavailable', gate + ' gate has not passed', gate + ' failed', *({'the vision comparison with the anchor failed'} if gate == 'vision_reference' else set())}]
                umbrella = {'anchor': 'start_image'}.get(rev['stage'], rev['stage'])
                gates = item['evidence'].get('gates', {})
                failed = {name for name, value in gates.items() if not (value is True or isinstance(value, dict) and value.get('passed') is True)}
                if gate in failed and failed <= {gate, umbrella}:
                    remaining = [r for r in remaining if r not in {umbrella + ' failed or unavailable', umbrella + ' gate has not passed', umbrella + ' failed'}]
            reasons.extend(remaining)
        except (ValueError, OSError) as exc:
            reasons.append(str(exc))
    return {'ready': not reasons, 'reasons': reasons}


def record_decision(project_root, character_folder, *, revision_id, decision, actor, reason,
                    expected_version, gate=None, operation=None, run_id=None):
    if decision not in {'accepted', 'rejected', 'accepted_with_exception'} or not actor.strip() or not reason.strip():
        raise ValueError('valid decision, actor and reason required')
    def action(root, state, data):
        from uuid import uuid4
        rev = state['revisions'].get(revision_id)
        if not rev:
            raise ValueError('unknown revision')
        if decision == 'accepted_with_exception':
            if gate not in {'loop_smoothness', 'symmetry', 'walk_mechanics', 'direction_set', 'vision_reference'} or operation != rev['operation']:
                raise ValueError('exception requires scoped artistic gate and exact operation')
            verdicts = [f['evidence'].get('gates', {}).get(gate) for f in rev['files'].values() if f['evidence']]
            if (not any(isinstance(v, dict) and v.get('passed') is False for v in verdicts)
                    or any(isinstance(v, dict) and (v.get('status') in {'unavailable', 'error'} or v.get('metrics', {}).get('status') in {'unavailable', 'error'}) for v in verdicts)):
                raise ValueError('exception requires actual failed evidence')
            if gate == 'vision_reference':
                from .gates.acceptance import validate, revision_context, validation_record
                from .gates.records import load_record, review_problem
                prior = [d for d in state['decisions'] if d['revision_id'] == revision_id]
                if prior and prior[-1]['decision'] == 'rejected':
                    raise ValueError('human rejection cannot be waived')
                for item in rev['files'].values():
                    path = _safe_file(root, item['path'])
                    if load_record(path) != item['evidence']:
                        raise ValueError('gate evidence changed; register a new revision')
                    context = revision_context(path, item['evidence'])
                    evidence = validation_record(item['evidence'], context)
                    if 'vision_reference' not in evidence.get('gates', {}) and review_problem(path) is None:
                        # The anchor view is explicitly exempt from comparison with itself.
                        from .gates.records import dependency_problems
                        if dependency_problems(evidence) or evidence.get('rejected'):
                            raise ValueError('anchor evidence changed or rejected')
                        continue
                    validate(path, evidence, gate, allow_passed=True)
        if decision != 'rejected':
            for item in rev['files'].values():
                if _hash(_safe_file(root, item['path'])) != item['sha256']:
                    raise ValueError('revision bytes changed')
        from datetime import datetime, timezone
        entry = {'decision_id': uuid4().hex, 'revision_id': revision_id, 'decision': decision,
                 'time': datetime.now(timezone.utc).isoformat(),
                 'actor': actor, 'reason': reason, 'gate': gate, 'operation': operation or rev['operation'],
                 'provider': rev['provider'], 'sha256': next(iter(rev['files'].values()))['sha256'],
                 'artifact_hashes': {k: v['sha256'] for k, v in rev['files'].items()}}
        state['decisions'].append(entry)
        if decision == 'rejected':
            _remove_invalid_selections(root, state, data)
        return entry, entry.copy()
    return _mutate(project_root, character_folder, expected_version, action, 'human.decision', run_id)


def _remove_invalid_selections(root, state, data):
    for stage in ('anchor', 'views', 'anim'):
        rid = state['current'].get(stage)
        if rid and not _readiness(root, state, state['revisions'][rid])['ready']:
            state['current'].pop(stage)
            for key in {'anchor': ('anchor',), 'views': ('views_dir',), 'anim': ('animation_paths',)}[stage]:
                data.pop(key, None)
            data['identity'] = {}


def promote(project_root, character_folder, *, revision_id, expected_version, actor, run_id=None):
    if not actor.strip():
        raise ValueError('actor required')
    def action(root, state, data):
        rev = state['revisions'].get(revision_id)
        if not rev:
            raise ValueError('unknown revision')
        decisions = [d for d in state['decisions'] if d['revision_id'] == revision_id]
        if not decisions or decisions[-1]['decision'] not in {'accepted', 'accepted_with_exception'}:
            raise ValueError('accepted human decision required')
        ready = _readiness(root, state, rev)
        if not ready['ready']:
            raise ValueError('promotion blocked: ' + '; '.join(ready['reasons']))
        stage = rev['stage']; state['current'][stage] = revision_id
        _remove_invalid_selections(root, state, data)
        paths = [f['path'] for f in rev['files'].values()]
        if stage == 'anchor':
            data['anchor'] = paths[0]
        elif stage == 'views':
            parents = {str(Path(p).parent).replace('\\', '/') for p in paths}
            if len(parents) != 1:
                raise ValueError('selected views must share one directory')
            directory = root / next(iter(parents))
            actual = {p.resolve() for p in directory.iterdir() if p.suffix.lower() == '.png'}
            if actual != {(root / p).resolve() for p in paths}:
                raise ValueError('views directory contains unselected or missing artifacts')
            # Route and expected facings are design inputs, not publication
            # pointers. Promotion must not silently rewrite those inputs and
            # invalidate its own ancestors; the revision retains its provider
            # and exact facings independently.
            data['views_dir'] = parents.pop()
        else:
            data['animation_paths'] = paths
        # Replace, never merge a stale native entity from another revision.
        data['identity'] = {rev['provider']: dict(rev['native_ids'])} if rev['native_ids'] else {}
        data.pop('anchor_asset_id', None)
        return None, {'revision_id': revision_id, 'actor': actor, 'stage': stage}
    return _mutate(project_root, character_folder, expected_version, action, 'pipeline.promoted', run_id)
