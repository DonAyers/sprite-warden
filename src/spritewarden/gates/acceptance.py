"""Scoped human artistic decisions; measurements remain immutable."""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def evidence_hash(record):
    from .records import _fingerprint
    return _fingerprint({k: v for k, v in record.items() if k != 'human_decision'})


def validate(image, record, gate='vision_reference', *, allow_passed=False):
    from .records import sha256_of, dependency_problems, canonical_stage
    if gate != 'vision_reference':
        raise ValueError('only vision_reference artistic findings can be accepted')
    if not record or record.get('sha256') != sha256_of(image):
        raise ValueError('missing or stale gate evidence')
    if record.get('rejected') or record.get('decision') == 'rejected':
        raise ValueError('human rejection cannot be waived')
    if dependency_problems(record):
        raise ValueError('dependency changed or unavailable')
    gates = record.get('gates', {})
    vision = gates.get(gate)
    if (not isinstance(vision, dict) or vision.get('passed') is not False and not (allow_passed and vision.get('passed') is True)
            or vision.get('status') in {'unavailable', 'error', 'not_applicable'}
            or vision.get('metrics', {}).get('status') in {'unavailable', 'error'}
            or vision.get('passed') is False and vision.get('status') != 'fail'
               and vision.get('metrics', {}).get('status') != 'mismatch'):
        raise ValueError('acceptance requires completed failed vision_reference evidence')
    stage = canonical_stage(record.get('stage') or record.get('params', {}).get('command'))
    umbrella = {'views'} if stage == 'views' else {stage}
    if stage == 'views':
        pixel_set = gates.get('direction_set')
        if not (pixel_set is True or isinstance(pixel_set, dict) and pixel_set.get('passed') is True
                and pixel_set.get('status') not in {'fail', 'unavailable', 'error', 'not_applicable'}
                and pixel_set.get('metrics', {}).get('status') not in {'unavailable', 'error'}):
            raise ValueError('direction_set completed passing pixel evidence required')
    for name in umbrella:
        verdict = gates.get(name)
        if isinstance(verdict, dict) and verdict.get('status') in {'unavailable', 'error'}:
            raise ValueError(name + ' unavailable')
    for name, verdict in gates.items():
        if name == gate or name in umbrella:
            continue
        if not (verdict is True or isinstance(verdict, dict) and
                (verdict.get('status') == 'not_applicable' or
                 verdict.get('passed') is True and verdict.get('status') not in {'fail', 'unavailable', 'error'})):
            raise ValueError(name + ' non-artistic failure cannot be waived')
    for name in record.get('params', {}).get('required_evidence', []):
        if name not in gates:
            raise ValueError(name + ' required evidence missing')


def revision_context(image, record):
    """Resolve exact immutable evidence; never trust a sidecar over revision state."""
    from ..enforce.config import find_project_root
    from ..workflow_state import design_fingerprint
    image = Path(image).resolve()
    for folder in (image.parent, *image.parents):
        pipeline = folder / 'pipeline.json'
        if not pipeline.is_file():
            continue
        data = json.loads(pipeline.read_text(encoding='utf-8'))
        if '_workflow' not in data:
            return None
        root = find_project_root(folder).resolve()
        state = data['_workflow']
        if not any((root / rev['pipeline_path']).resolve() == pipeline.resolve() for rev in state['revisions'].values()):
            roots = {parent for parent in (folder, *folder.parents) if any(
                (parent / rev['pipeline_path']).resolve() == pipeline.resolve() for rev in state['revisions'].values())}
            if len(roots) != 1:
                raise ValueError('revision project root is ambiguous')
            root = roots.pop()
        matches = [rev for rev in state['revisions'].values() if any(
            (root / item['path']).resolve() == image and item['evidence'] == record
            for item in rev['files'].values())]
        selected = [rev for rev in matches if state['current'].get(rev['stage']) == rev['revision_id']]
        matches = selected or matches
        if len(matches) != 1:
            raise ValueError('exact registered revision required; selection missing or ambiguous')
        rev = matches[0]
        if design_fingerprint(data) != rev['design_hash'] or not rev.get('pipeline_evidence_current'):
            raise ValueError('revision design or pipeline evidence changed')
        decisions = [d for d in state['decisions'] if d['revision_id'] == rev['revision_id']]
        if decisions and decisions[-1]['decision'] == 'rejected':
            raise ValueError('human rejection cannot be waived')
        return root, folder, state, rev, decisions[-1] if decisions else None
    return None


def validation_record(record, context):
    if not context:
        return record
    import copy
    result = copy.deepcopy(record)
    result.get('dependencies', {}).pop('pipeline', None)
    return result


def active(image, record):
    """Return an evidence-bound artistic exception, never a replacement verdict."""
    if not isinstance(record, dict):
        return None
    try:
        context = revision_context(image, record)
    except (ValueError, OSError, KeyError):
        return None
    entry = context[-1] if context else record.get('human_decision')
    if not entry or entry.get('decision') != 'accepted_with_exception':
        return None
    try:
        validate(image, validation_record(record, context), entry.get('gate'))
        if context:
            from ..workflow_state import _readiness, _safe_file, _hash
            from .records import load_record
            root, _, state, rev, _ = context
            for item in rev['files'].values():
                path = _safe_file(root, item['path'])
                if _hash(path) != item['sha256'] or load_record(path) != item['evidence']:
                    return None
            for dep in rev['dependencies'].values():
                ancestor = state['revisions'].get(dep)
                if not ancestor or state['current'].get(ancestor['stage']) != dep or not _readiness(root, state, ancestor)['ready']:
                    return None
            return entry if entry.get('operation') == rev['operation'] else None
        if entry.get('evidence_hash') != evidence_hash(record):
            return None
    except (ValueError, OSError, TypeError):
        return None
    return entry


def accept(paths, *, actor, reason, gate='vision_reference'):
    """Record explicit human review using authoritative revision state when present.

    Gallery callers must authenticate the human interaction before invoking this
    API. Returned decisions retain scope/rationale without modifying measurements.
    """
    if gate != 'vision_reference':
        raise ValueError('only vision_reference artistic findings can be accepted')
    from .records import _expand, load_record, record_path, stage_readiness
    from .. import events
    from ..enforce.config import find_project_root
    if not actor.strip() or not reason.strip():
        raise ValueError('named actor and reason required')
    images = list(dict.fromkeys(p.resolve() for p in _expand(paths)))
    if not images:
        raise ValueError('no image files found')
    snapshots = {image: load_record(image) for image in images}
    contexts = [revision_context(image, snapshots[image]) for image in images]
    if any(contexts):
        if not all(contexts) or len({c[3]['revision_id'] for c in contexts}) != 1:
            raise ValueError('acceptance must target one exact revision')
        root, folder, state, rev, _ = contexts[0]
        selected = {(root / item['path']).resolve() for item in rev['files'].values()}
        if selected != set(images):
            raise ValueError('acceptance must name all artifacts of the revision')
        from ..workflow_state import record_decision
        entry = record_decision(root, folder, revision_id=rev['revision_id'],
            decision='accepted_with_exception', actor=actor, reason=reason, gate=gate,
            operation=rev['operation'], expected_version=state['version'])
        return [{'image': str(image), **entry} for image in images]
    planned = []
    for image in images:
        record = load_record(image)
        if record and stage_readiness(image, record.get('stage', 'views'))['ready']:
            continue  # Already satisfied; do not invent an acceptance of passing evidence.
        validate(image, record, gate)
        entry = {'decision_id': uuid4().hex, 'decision': 'accepted_with_exception',
                 'actor': actor, 'reason': reason, 'gate': gate,
                 'sha256': record['sha256'], 'evidence_hash': evidence_hash(record),
                 'time': datetime.now(timezone.utc).isoformat(),
                 'operation': record.get('params', {}).get('operation') or record.get('stage')}
        planned.append((image, record, entry))
    payload = {'actor': actor, 'reason': reason, 'gate': gate,
               'decisions': [{'image': str(p), **e} for p, _, e in planned]}
    root = find_project_root(images[0].parent)
    if events.emit('human.decision.intent', project_root=root, payload=payload) is None:
        raise RuntimeError('event ledger unavailable; evidence unchanged')
    for image, original in snapshots.items():
        if load_record(image) != original:
            raise ValueError('gate evidence changed during acceptance')
        if image not in {p for p, _, _ in planned} and not stage_readiness(image, original.get('stage', 'views'))['ready']:
            raise ValueError('passing evidence changed during acceptance')
    for image, record, _ in planned:
        if load_record(image) != record:
            raise ValueError('gate evidence changed during acceptance')
        validate(image, record, gate)
    for image, record, entry in planned:
        record['human_decision'] = entry
        record_path(image).write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8')
    events.emit('human.decision', project_root=root, payload=payload)
    return payload['decisions']
