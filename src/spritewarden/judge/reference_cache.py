"""Immutable default reference evidence plus explicit, append-only rechecks."""
from pathlib import Path
from uuid import uuid4
import hashlib
import json
import os


def compare(judge, candidate, reference, params, compute, *, refresh=False):
    from .vision import ReferenceComparison, reference_prompt, prepare_pair_for_vision, VISION_SAMPLING
    from ..repair import digest
    from ..events import sanitize, emit, _PROJECT
    prompt = reference_prompt(candidate_facing=params['facing'] or params['anchor_facing'], anchor_facing=params['anchor_facing'],
                              description=params['description'], expected_perspective=params['expected_perspective'], faces=params['faces'])
    board = prepare_pair_for_vision(reference, candidate, zoom=params['zoom'], labels=('ANCHOR','CANDIDATE'))
    identity = {'candidate': digest(candidate), 'reference': digest(reference), 'params': params,
                'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(), 'board_digest': digest(board),
                'model': judge.vision_model, 'sampling': dict(VISION_SAMPLING), 'response_format': {'type':'json_object'},
                'implementation': hashlib.sha256(Path(__file__).with_name('vision.py').read_bytes()).hexdigest()}
    key = hashlib.sha256(json.dumps(identity, sort_keys=True, default=str).encode()).hexdigest()
    base = Path(os.environ.get('SPRITEWARDEN_VISION_CACHE') or Path(_PROJECT.get() or Path.cwd()) / '.spritewarden' / 'vision-cache')
    folder = base / key
    default = folder / 'default.json'
    def load_default():
        record = json.loads(default.read_text(encoding='utf-8'))
        result = ReferenceComparison.model_validate(record['result'])
        if record.get('comparison_key') != key or result.request_fingerprint != key or result.status not in {'match', 'mismatch'}:
            raise ValueError('Cached reference receipt is invalid; inspect it before any new paid request')
        return result
    if default.exists() and not refresh:
        result = load_default()
        emit('vision.reused', payload={'comparison_key': key, 'model': judge.vision_model, 'cost_usd': 0})
        return result
    folder.mkdir(parents=True, exist_ok=True)
    lock = folder / 'in-flight.lock'
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise RuntimeError('Reference comparison is in flight or interrupted; reconcile before retrying')
    os.close(fd)
    try:
        if default.exists() and not refresh:
            return load_default()
        if not emit('vision.started', payload={'comparison_key': key, 'model': judge.vision_model, 'refresh': refresh}):
            raise RuntimeError('Vision intent capture failed; no model request submitted')
        result = compute()
        completed = result.status in {'match', 'mismatch'}
        result = ReferenceComparison.model_validate(sanitize(result.model_dump())).model_copy(
            update={'request_fingerprint':key,'request_audit':sanitize(identity)})
        record = {'comparison_key': key, 'refresh': refresh, 'result': result.model_dump()}
        history = folder / 'history'; history.mkdir(exist_ok=True)
        history_file = history / (str(uuid4()) + '.json')
        history_file.write_text(json.dumps(record, indent=2), encoding='utf-8')
        if not refresh and completed:
            with default.open('x', encoding='utf-8') as stream:
                json.dump(record, stream, indent=2)
        event_type = ('vision.rechecked' if refresh else 'vision.completed') if completed else 'vision.assessment_failed'
        emit(event_type, payload={'comparison_key': key, 'model': judge.vision_model,
             'status': result.status, 'cost_usd': result.cost_usd, 'prior_default_preserved': refresh})
        return result
    finally:
        lock.unlink(missing_ok=True)
