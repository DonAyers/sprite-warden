"""Explicit local authorization for native PixelLab rotation replacement.

This does not execute MCP, upload images, or assert remote ownership. Operators
supply downloaded upload/readback files; remote mutation remains with their client.
"""
from pathlib import Path
from datetime import datetime, timezone, timedelta
from uuid import UUID, uuid4
import hashlib
import json
import re


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def within(root, path):
    root=Path(root).resolve();p=Path(path);p=(p if p.is_absolute() else root/p).resolve()
    if not p.is_relative_to(root):raise ValueError('publication file outside project')
    return p


def target_file(root, target):
    from .enforce.core import _Manifest
    from .enforce.config import load_config
    match=re.fullmatch(r'character:([0-9a-fA-F-]{36}):(south|south-east|east|north-east|north|north-west|west|south-west)',target or '')
    if not match:raise ValueError('only explicit registered character rotation targets are supported')
    UUID(match[1])
    folder=_Manifest(load_config(root)).character_dir(match[1])
    if folder is None:raise ValueError('target character is not registered')
    folder=within(root,folder)
    direction=match[2]
    path=folder/(direction+'.png')
    if not path.is_file():
        aliases=list(folder.glob('*.png'))
        from .judge.views import canonical_facing
        aliases=[p for p in aliases if canonical_facing(p.stem)==canonical_facing(direction)]
        if len(aliases)!=1:raise ValueError('target rotation missing or ambiguous')
        path=aliases[0]
    return within(root,path),direction


def validate_image_reference(image):
    if not isinstance(image,str) or not re.fullmatch(r'upload:[0-9a-fA-F-]{36}',image):
        raise ValueError('publication currently requires an explicit upload:UUID reference')
    UUID(image.split(':')[1])


def source_snapshot(root, source):
    from .gates.records import load_record, stage_readiness, sha256_of
    from .enforce.core import find_pipeline
    from .workflow_state import source_revision_reasons
    source=within(root,source)
    ready=stage_readiness(source,'views')
    if not ready['ready']:raise ValueError('publication source is not ready: '+'; '.join(ready['reasons']))
    reasons=source_revision_reasons(root,source)
    if reasons:raise ValueError('publication revision is not ready: '+'; '.join(reasons))
    return {'source':str(source),'sha256':sha256_of(source),'evidence_hash':fingerprint(load_record(source))}


def prepare(root,source,image,target,*,uploaded_file,actor,reason):
    from PIL import Image
    from .repair import digest
    from . import events
    root=Path(root).resolve();validate_image_reference(image)
    if not isinstance(actor,str) or not actor.strip() or not isinstance(reason,str) or not reason.strip():raise ValueError('reviewer and reason required')
    if len(actor)>200 or len(reason)>2000:raise ValueError('reviewer or reason too long')
    folder=within(root,root/'.spritewarden/publications')
    source=within(root,source);uploaded_file=within(root,uploaded_file)
    target_path,direction=target_file(root,target)
    with Image.open(source) as a,Image.open(uploaded_file) as uploaded,Image.open(target_path) as old:
        if digest(a)!=digest(uploaded):raise ValueError('uploaded readback does not match the local source pixels')
        if a.size!=old.size:raise ValueError('replacement canvas differs from registered target')
        pixels=digest(a);size=list(a.size)
    snapshot=source_snapshot(root,source)
    from .gates.records import load_record
    from .judge.views import canonical_facing
    recorded_facing = ((load_record(source) or {}).get('params') or {}).get('facing')
    if canonical_facing(str(recorded_facing or '')) != canonical_facing(direction):
        raise ValueError('reviewed source facing does not match target rotation')
    record=events.sanitize({**snapshot,'root':str(root),'id':str(uuid4()),'operation':'save_to_asset','provider':'pixellab','image':image,'target':target,
            'target_path':str(target_path),'target_sha256':hashlib.sha256(target_path.read_bytes()).hexdigest(),
            'pixel_digest':pixels,'size':size,'actor':actor,'reason':reason,'status':'authorized',
            'expires':(datetime.now(timezone.utc)+timedelta(hours=24)).isoformat()})
    if not isinstance(record,dict):raise RuntimeError('publication authorization metadata could not be sanitized')
    record['fingerprint']=fingerprint(record)
    if not events.emit('publication.authorized',project_root=root,run_id=record['id'],payload={'fingerprint':record['fingerprint'],'target':target,'actor':actor,'reason':reason}):raise RuntimeError('publication authorization capture failed')
    if source_snapshot(root,source)!=snapshot:raise ValueError('publication source changed during authorization')
    folder.mkdir(parents=True,exist_ok=True)
    temporary=folder/(record['id']+'.tmp');temporary.write_text(json.dumps(record,indent=2),encoding='utf-8');temporary.replace(folder/(record['id']+'.json'))
    return record


def validate_receipt(root,record,args,*,allow_expired=False,allow_closed=False):
    from . import events
    original=dict(record);proof=original.pop('fingerprint',None)
    if proof!=fingerprint(original):raise ValueError('publication receipt changed')
    if record.get('root')!=str(Path(root).resolve()):raise ValueError('publication receipt belongs to another project')
    if record.get('status')!='authorized':raise ValueError('publication receipt is not authorized')
    if not allow_expired and datetime.now(timezone.utc)>=datetime.fromisoformat(record['expires']):raise ValueError('publication receipt expired')
    allowed = {'image', 'target', 'description'}
    if args.get('image')!=record['image'] or args.get('target')!=record['target'] or set(args)-allowed or args.get('description') not in (None, ''):raise ValueError('publication arguments do not match authorization')
    path,direction=target_file(root,args['target'])
    if str(path)!=record['target_path'] or hashlib.sha256(path.read_bytes()).hexdigest()!=record['target_sha256']:raise ValueError('registered target changed')
    snapshot=source_snapshot(root,record['source'])
    if any(snapshot[k]!=record[k] for k in snapshot):raise ValueError('publication source or evidence changed')
    store=events.EventStore(events.default_path(root),read_only=True)
    rows = store.list_events(run_id=record['id'],limit=1000)
    if any(e['event_type']=='publication.cancelled' for e in rows):
        raise ValueError('publication authorization revoked in the ledger')
    if not allow_closed and any(e['event_type']=='publication.verified' for e in rows):
        raise ValueError('publication authorization already closed in the ledger')
    if not any(e['event_type']=='publication.authorized' and e['payload'].get('fingerprint')==proof for e in rows):
        raise ValueError('publication authorization has no durable receipt')
    return record


def reasons(root,args):
    try:
        validate_image_reference(args.get('image'));target_file(root,args.get('target'))
    except (ValueError,OSError,TypeError) as exc:return [str(exc)]
    folder=Path(root)/'.spritewarden/publications'
    problems=[]
    for path in folder.glob('*.json'):
        try:
            record=json.loads(path.read_text(encoding='utf-8'))
            if record.get('image')!=args.get('image') or record.get('target')!=args.get('target'):continue
            validate_receipt(root,record,args);return []
        except (ValueError,OSError,KeyError,TypeError) as exc:problems.append(str(exc))
    return problems or ['No matching publication authorization; run warden publish prepare after reviewing the exact replacement']


def verify_character(root,character_id,download_dir):
    from .publication_sync import verify
    return verify(root,character_id,download_dir)


def character_sync_reasons(root,character_id):
    from .publication_sync import reasons
    return reasons(root,character_id)


def cancel(root,receipt_id,*,actor,reason):
    from .publication_sync import cancel as revoke
    return revoke(root,receipt_id,actor=actor,reason=reason)


def confirm(root,receipt_id,readback):
    from PIL import Image
    from .repair import digest
    from . import events
    UUID(receipt_id);path=within(root,Path(root)/'.spritewarden/publications'/(receipt_id+'.json'))
    record=json.loads(path.read_text(encoding='utf-8'))
    validate_receipt(root,record,{'image':record['image'],'target':record['target']},allow_expired=True,allow_closed=True)
    with Image.open(within(root,readback)) as result:
        if digest(result)!=record['pixel_digest']:raise ValueError('remote readback pixels do not match authorized replacement')
    record['status']='verified';record['verified_at']=datetime.now(timezone.utc).isoformat()
    record.pop('fingerprint');record['fingerprint']=fingerprint(record)
    if not events.emit('publication.verified',project_root=root,run_id=receipt_id,payload={'target':record['target'],'pixel_digest':record['pixel_digest'],
                       'fingerprint':record['fingerprint'],'readback_supplied_by_operator':True}):raise RuntimeError('publication verification capture failed')
    tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(record,indent=2),encoding='utf-8');tmp.replace(path)
    return {'status':'verified','id':receipt_id,'target':record['target'],
            'gates':{'publication_readback':{'passed':True,'pixel_digest':record['pixel_digest'],'operator_supplied':True}},
            'notice':'Operator-supplied readback verified; no provider request was made by Warden'}
