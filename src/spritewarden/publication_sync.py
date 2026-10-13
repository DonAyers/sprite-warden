"""Operator-supplied complete-character readback, bound to local evidence."""
from pathlib import Path
from datetime import datetime, timezone, timedelta
from uuid import UUID
import json
from PIL import Image
from . import publication as pub, events
from .repair import digest


def _facts(root,cid):
    folder=pub.within(root,Path(root)/'.spritewarden/publications')
    result={}
    for path in folder.glob('*.json'):
        if path.stat().st_size>100000:raise ValueError('publication receipt exceeds size limit')
        record=json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(record,dict):raise ValueError('publication receipt must be a JSON object')
        if not str(record.get('target','')).startswith('character:'+cid+':'):continue
        if record.get('root')!=str(Path(root).resolve()):raise ValueError('publication receipt belongs to another project')
        if record.get('id')!=path.stem:raise ValueError('publication receipt ID differs from filename')
        body=dict(record);proof=body.pop('fingerprint',None)
        if proof!=pub.fingerprint(body):raise ValueError('publication receipt changed')
        kinds={'authorized':'publication.authorized','verified':'publication.verified','cancelled':'publication.cancelled'}
        kind=kinds.get(record['status'])
        if not kind:raise ValueError('unknown publication state')
        store=events.EventStore(events.default_path(root),read_only=True)
        if not any(e['event_type']==kind and e['payload'].get('fingerprint')==proof for e in store.list_events(run_id=record['id'],limit=1000)):
            raise ValueError('publication receipt lacks matching ledger proof')
        result[record['id']]={'status':record['status'],'fingerprint':proof}
    return result


def _views(root,folder):
    from .judge.views import canonical_facing, GAME_ORDER, GAME_ORDER_4
    mapped={}
    for path in Path(folder).glob('*.png'):
        facing=canonical_facing(path.stem)
        if not facing:continue
        path=pub.within(root,path)
        if facing in mapped:raise ValueError('ambiguous rotation aliases: '+facing)
        mapped[facing]=path
    if set(mapped) not in (set(GAME_ORDER),set(GAME_ORDER_4)):
        raise ValueError('expected a complete four- or eight-direction set')
    return mapped


def _snapshot(root,cid):
    from .enforce.config import load_config
    from .enforce.core import _Manifest
    from .gates.records import load_record
    from .judge.views import canonical_facing
    folder=_Manifest(load_config(root)).character_dir(cid)
    if folder is None:raise ValueError('character not registered')
    sources=_views(root,pub.within(root,folder))
    state={}
    for facing,path in sources.items():
        record=load_record(path) or {}
        if canonical_facing(str((record.get('params') or {}).get('facing') or ''))!=facing:
            raise ValueError('registered source facing evidence does not match '+facing)
        state[facing]=pub.source_snapshot(root,path)
    return {'sources':state,'publications':_facts(root,cid)}


def verify(root,cid,download_dir):
    UUID(cid)
    directory=pub.within(root,download_dir)
    group='character:'+cid
    if not events.emit('publication.character_verification.started',project_root=root,run_id=group,
                       payload={'character_id':cid,'download_dir':str(directory),'operator_supplied':True}):
        raise RuntimeError('character verification capture failed')
    snapshot=_snapshot(root,cid)
    if any(f['status']=='authorized' for f in snapshot['publications'].values()):
        raise ValueError('confirm or cancel pending replacement receipts before character verification')
    downloads=_views(root,directory)
    if set(downloads)!=set(snapshot['sources']):raise ValueError('remote character facing set differs from registered local set')
    for facing,path in downloads.items():
        with Image.open(path) as remote,Image.open(snapshot['sources'][facing]['source']) as local:
            if digest(remote)!=digest(local):raise ValueError('remote '+facing+' pixels differ from the locally reviewed view')
    record={'character_id':cid,'snapshot':snapshot,'operator_supplied':True,'verified_at':datetime.now(timezone.utc).isoformat(),
            'expires':(datetime.now(timezone.utc)+timedelta(hours=6)).isoformat()}
    record['fingerprint']=pub.fingerprint(record)
    if not events.emit('publication.character_verified',project_root=root,run_id=group,payload={'character_id':cid,'fingerprint':record['fingerprint'],'operator_supplied':True}):
        raise RuntimeError('character verification receipt failed')
    if _snapshot(root,cid)!=snapshot:raise ValueError('local character changed during verification')
    folder=pub.within(root,Path(root)/'.spritewarden/publications/characters');folder.mkdir(parents=True,exist_ok=True)
    path=folder/(cid+'.json');temp=path.with_suffix('.tmp');temp.write_text(json.dumps(record,indent=2),encoding='utf-8');temp.replace(path)
    return {'status':'verified','character_id':cid,'facings':list(downloads),'operator_supplied_readback':True}


def reasons(root,cid):
    try:
        facts=_facts(root,cid)
        try:
            UUID(cid)
        except (ValueError,TypeError,AttributeError):
            if not facts:return []  # Legacy fixture/registration IDs are not newly constrained.
            raise ValueError('publication character ID must be a UUID')
        path=pub.within(root,Path(root)/'.spritewarden/publications/characters'/(cid+'.json'))
        if not facts and not path.exists():return []  # Legacy registrations remain compatible until opting in.
        if not path.is_file():raise ValueError('complete remote character readback required after publication')
        record=json.loads(path.read_text(encoding='utf-8'));proof=record.pop('fingerprint',None)
        if proof!=pub.fingerprint(record) or record.get('character_id')!=cid:raise ValueError('character verification receipt changed')
        if datetime.now(timezone.utc)>=datetime.fromisoformat(record['expires']):raise ValueError('remote character verification expired; fetch a fresh readback')
        if record['snapshot']!=_snapshot(root,cid):raise ValueError('local character or publication state changed since readback')
        store=events.EventStore(events.default_path(root),read_only=True)
        with store._connect() as conn:
            latest=conn.execute('SELECT event_type,payload FROM events WHERE run_id=? ORDER BY sequence DESC LIMIT 1',('character:'+cid,)).fetchone()
        if not latest or latest['event_type']!='publication.character_verified' or json.loads(latest['payload']).get('fingerprint')!=proof:
            raise ValueError('latest remote character verification is incomplete or disagrees')
        return []
    except (ValueError,OSError,KeyError,TypeError) as exc:
        return ['remote character sync: '+str(exc)]


def cancel(root,receipt_id,*,actor,reason):
    UUID(receipt_id)
    if not isinstance(actor,str) or not actor.strip() or not isinstance(reason,str) or not reason.strip():raise ValueError('reviewer and cancellation reason required')
    if len(actor)>200 or len(reason)>2000:raise ValueError('reviewer or reason too long')
    path=pub.within(root,Path(root)/'.spritewarden/publications'/(receipt_id+'.json'))
    record=json.loads(path.read_text(encoding='utf-8'))
    cid=record['target'].split(':')[1]
    facts=_facts(root,cid)
    if record['id'] not in facts or record['status']!='authorized':raise ValueError('only an authorized receipt can be cancelled')
    record.pop('fingerprint');record.update(status='cancelled',cancelled_at=datetime.now(timezone.utc).isoformat(),cancel_actor=actor,cancel_reason=reason)
    record=events.sanitize(record)
    if not isinstance(record,dict):raise RuntimeError('cancellation metadata could not be sanitized')
    record['fingerprint']=pub.fingerprint(record)
    if not events.emit('publication.cancelled',project_root=root,run_id=receipt_id,payload={'fingerprint':record['fingerprint'],'target':record['target'],'actor':actor,'reason':reason}):raise RuntimeError('cancellation capture failed')
    temp=path.with_suffix('.tmp');temp.write_text(json.dumps(record,indent=2),encoding='utf-8');temp.replace(path)
    return {'status':'cancelled','id':receipt_id,'notice':'Local authorization revoked; this does not undo any remote change. Verify the full character before deriving assets.'}
