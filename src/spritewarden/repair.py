"""Deterministic, pixel-exact repair. No model calls or implicit approval."""
import hashlib
from PIL import Image

MAX_PIXELS = 1024 * 1024


def save_candidate(root, source, expected_digest, mask, mapping, *, actor, reason, patch=None, repair_plan=None):
    """Save a new, unapproved candidate with a reversible, source-bound patch."""
    import json
    from pathlib import Path
    from uuid import uuid4
    from . import events
    root, source = Path(root).resolve(), Path(source).resolve()
    if not source.is_relative_to(root):
        raise ValueError('repair source outside project')
    if not isinstance(actor, str) or not actor.strip() or not isinstance(reason, str) or not reason.strip():
        raise ValueError('actor and repair reason required')
    if len(actor) > 200 or len(reason) > 2000:
        raise ValueError('actor or reason too long')
    def rgb(text):
        if not isinstance(text, str) or len(text) != 7 or text[0] != '#':
            raise ValueError('colors must be #RRGGBB')
        return tuple(bytes.fromhex(text[1:]))
    if not isinstance(mapping, dict) or len(mapping) > 256:
        raise ValueError('invalid color mapping')
    with Image.open(source) as opened:
        before = _image(opened)
    if digest(before) != expected_digest:
        raise ValueError('source changed; reload before repairing')
    after = apply_patch(before, patch) if patch is not None else recolor(before, mask, {rgb(k): rgb(v) for k, v in mapping.items()})
    patch = make_patch(before, after)
    if not patch['changes']:
        raise ValueError('repair changes no pixels')
    preflight=None
    alpha_changed=before.getchannel('A').tobytes()!=after.getchannel('A').tobytes()
    if alpha_changed and repair_plan is None:
        raise ValueError('silhouette/alpha repair requires an explicit repair plan; pose is not inferred from pixels')
    if repair_plan is not None:
        from .repair_preflight import inspect_plan, RepairPlan, _within
        plan=RepairPlan.model_validate(repair_plan)
        if _within(root,plan.source)!=source:raise ValueError('repair plan source differs from actual source')
        if alpha_changed and plan.defect_kind not in {'geometry','orientation'}:
            raise ValueError('alpha repair requires geometry/orientation plan')
        if plan.proposed_action not in {'color_patch','geometry_patch','donor_patch'}:
            raise ValueError('local repair cannot claim a provider generation plan')
        preflight=inspect_plan(root,plan.model_dump(),candidate_image=after)
        if preflight['status']=='blocked':raise ValueError('repair plan blocked: '+'; '.join(preflight['blockers']))
        repair_plan=plan.model_dump()
    run = str(uuid4())
    destination = source.parent / '.repairs' / run
    if not destination.resolve().is_relative_to(root):
        raise ValueError('repair destination outside project')
    payload = {'source': str(source), 'source_digest': expected_digest, 'actor': actor, 'reason': reason, 'changed_pixels': len(patch['changes'])}
    if preflight:
        payload.update({'repair_plan':repair_plan,'preflight':preflight})
    else:
        payload.update({'preflight_status':'not_supplied','pose_review_status':'not_assessed'})
    if not events.emit('repair.started', project_root=root, run_id=run, payload=payload):
        raise RuntimeError('repair intent capture failed')
    with Image.open(source) as opened:
        if digest(opened) != expected_digest:
            raise ValueError('source changed during repair capture')
    if preflight:
        dependencies=[{'image':str(source),'sha256':preflight['metrics']['source_sha256']},*preflight['references']]
        if preflight.get('donor'):dependencies.append(preflight['donor'])
        if plan.mask:dependencies.append({'image':str(_within(root,plan.mask)),'sha256':preflight['metrics']['mask_sha256']})
        for dependency in dependencies:
            checked=Path(dependency['image']).resolve()
            if not checked.is_relative_to(root) or hashlib.sha256(checked.read_bytes()).hexdigest()!=dependency['sha256']:
                raise ValueError('repair dependency changed during intent capture; inspect current inputs')
    destination.mkdir(parents=True, exist_ok=False)
    target = destination / source.name
    after.save(target, 'PNG')
    if preflight:
        preflight['candidate']=str(target)
    (destination / 'patch.json').write_text(json.dumps(patch, indent=2), encoding='utf-8')
    from .judge.pixel import check_pixel_grid
    grid, _ = check_pixel_grid(after)
    validation = {'pixel_grid': grid.model_dump(), 'alpha_preserved': before.getchannel('A').tobytes() == after.getchannel('A').tobytes(),
                  'dimensions_preserved': before.size == after.size, 'scope': 'local repair checks only; stage evidence and artistic approval must be refreshed'}
    receipt = {**payload, 'path': str(target), 'approval': 'pending', 'run_id': run, 'validation': validation}
    (destination / 'repair.json').write_text(json.dumps(events.sanitize(receipt), indent=2), encoding='utf-8')
    events.emit('repair.completed', project_root=root, run_id=run, payload=receipt)
    return receipt


def digest(image):
    image = image.convert('RGBA')
    return hashlib.sha256(str(image.size).encode() + image.tobytes()).hexdigest()


def _image(image):
    if image.width * image.height > MAX_PIXELS:
        raise ValueError('repair images exceed pixel limit')
    return image.convert('RGBA')


def recolor(image, mask, mapping):
    out = _image(image).copy()
    for x, y in mask:
        if type(x) is not int or type(y) is not int or not (0 <= x < out.width and 0 <= y < out.height):
            raise ValueError('mask pixel outside image')
        old = out.getpixel((x, y))
        replacement = mapping.get(old[:3])
        if replacement is not None:
            if len(replacement) != 3 or any(type(v) is not int or not 0 <= v <= 255 for v in replacement):
                raise ValueError('replacement must be RGB bytes')
            out.putpixel((x, y), (*replacement, old[3]))
    return out


def make_patch(before, after):
    before, after = _image(before), _image(after)
    if before.size != after.size:
        raise ValueError('example must have the same dimensions')
    changes = []
    for y in range(before.height):
        for x in range(before.width):
            old, new = before.getpixel((x, y)), after.getpixel((x, y))
            if old != new:
                changes.append([x, y, list(old), list(new)])
    return {'version': 1, 'source_digest': digest(before), 'size': list(before.size), 'changes': changes}


def apply_patch(image, patch):
    out = _image(image).copy()
    if patch.get('version') != 1 or patch.get('source_digest') != digest(out) or patch.get('size') != list(out.size):
        raise ValueError('patch source does not match')
    for x, y, old, new in patch['changes']:
        if type(x) is not int or type(y) is not int or not (0 <= x < out.width and 0 <= y < out.height):
            raise ValueError('patch coordinate outside source')
        if list(out.getpixel((x, y))) != old or len(new) != 4 or any(type(v) is not int or not 0 <= v <= 255 for v in new):
            raise ValueError('patch source pixel or replacement invalid')
        out.putpixel((x, y), tuple(new))
    return out
