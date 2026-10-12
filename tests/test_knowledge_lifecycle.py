import json
import pytest
from spritewarden.learning.db import LearningDB
from spritewarden.learning.models import Source
from spritewarden.learning.acquisition import check_source, FetchResult


def test_raised_fetch_failure_is_persisted(tmp_path):
    db=LearningDB(tmp_path/'learn.db'); s=Source(id='s',provider='p',kind='md',url='https://example.test')
    db.upsert_source(s)
    def unavailable(*args,**kwargs): raise OSError('network unavailable')
    assert check_source(s,db,fetch_fn=unavailable)==[]
    assert db.source_health()[0]['successful']==0
    assert 'network unavailable' in db.source_health()[0]['error']


def test_cli_mark_rejected_without_opening_database(tmp_path, capsys, monkeypatch):
    import argparse
    from spritewarden.learning.cli import register
    monkeypatch.setenv('SPRITEWARDEN_LEARNING_DB',str(tmp_path/'never.db'))
    parser=argparse.ArgumentParser(); register(parser.add_subparsers())
    args=parser.parse_args(['learn','export-tips','--mark'])
    assert args.func(args)==1
    assert not (tmp_path/'never.db').exists()


def test_existing_title_requires_matching_content_before_receipt(tmp_path):
    from spritewarden.learning.recipe_tips import build_recipe_tips, publish_tips
    tip=build_recipe_tips({'providers':{'demo':{'operations':{}}}})[0]
    db=LearningDB(tmp_path/'learn.db')
    class Session:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def call_tool(self,name,args):
            if name=='search': return [{'id':3,'title':tip['title']}]
            return {'id':3,'uuid':'a1111111-1111-4111-8111-111111111111','title':tip['title'],'solution':'conflicting content'}
    result=publish_tips([tip],db,command=['fake'],dry_run=False,session_factory=lambda _:Session())
    assert result[0]['action']=='error'
    assert not db.is_tip_exported(tip['export_key'])


def test_strategy_blind_export_mark_is_refused(tmp_path):
    from spritewarden.learning.tipshare_bridge import export_tips
    with pytest.raises(ValueError,match='verified'):
        export_tips(LearningDB(tmp_path/'learn.db'),mark=True)


def test_database_connections_close_on_context_exit(tmp_path):
    import sqlite3
    db=LearningDB(tmp_path/'learn.db')
    with db._get_conn() as conn:
        conn.execute('SELECT 1')
    with pytest.raises(sqlite3.ProgrammingError): conn.execute('SELECT 1')


def test_real_tipshare_sync_loop_isolated(tmp_path):
    import os
    from pathlib import Path
    from spritewarden.learning.recipe_tips import build_recipe_tips, publish_tips
    from spritewarden.learning.tipshare_bridge import sync_outcomes
    from spritewarden.learning.mcp_probe import StdioSession
    node=os.environ.get('TIPSHARE_TEST_NODE')
    server=os.environ.get('TIPSHARE_TEST_SERVER')
    if not node or not server:
        pytest.skip('Set explicit Node 22 and built Tipshare server paths for isolated integration')
    command=[node,server]
    def factory(cmd): return StdioSession(cmd,env={'TIPSHARE_DB':str(tmp_path/'tips.db'),'TIPSHARE_AGENT_ID':'test'})
    db=LearningDB(tmp_path/'learn.db')
    registry={'version':1,'providers':{'demo':{'operations':{}}}}
    first=build_recipe_tips(registry)
    sent=publish_tips(first,db,command=command,dry_run=False,session_factory=factory)
    assert sent[0]['action']=='published',sent
    with factory(command) as session:
        tip=session.call_tool('get_tip',{'tip_id':sent[0]['tip_ref']})
        session.call_tool('mark_outcome',{'tip_id':tip['id'],'worked':False,'note':'narrow applicability'})
    got=sync_outcomes(db,command=command,session_factory=factory,dry_run=False)
    assert got['changed']==1 and got['proposals'][0]['how']=='narrow applicability'
    registry['pipeline_rules']=['new scoped rule']
    changed=build_recipe_tips(registry)
    replacement=publish_tips(changed,db,command=command,dry_run=False,session_factory=factory)
    assert replacement[0]['action']=='published',replacement
    with factory(command) as session:
        old=session.call_tool('get_tip',{'tip_id':sent[0]['tip_ref']})
        assert old['status']=='superseded'
        new=session.call_tool('get_tip',{'tip_id':replacement[0]['tip_ref']})
        assert new['supersedes']==old['uuid']
        assert new['applicability']['registry_hash']==changed[0]['content_hash']
    assert publish_tips(changed,db,command=command,dry_run=False,session_factory=factory)[0]['action']=='skipped_exported'


def test_lifecycle_cli_defaults_to_review_only(tmp_path, capsys, monkeypatch):
    import argparse
    from spritewarden.learning.cli import register
    monkeypatch.setenv('SPRITEWARDEN_LEARNING_DB',str(tmp_path/'learn.db'))
    parser=argparse.ArgumentParser(); register(parser.add_subparsers())
    args=parser.parse_args(['learn','corrections','--curated','--dry-run','--json'])
    assert args.func(args)==0
    result=json.loads(capsys.readouterr().out)
    assert result['changed']==5 and result['dry_run'] is True
    assert not (tmp_path/'learn.db').exists(), 'dry-run must not create/migrate the configured database'
    assert not LearningDB(tmp_path/'learn.db').list_proposals()
    dump=tmp_path/'dump.json'; dump.write_text(json.dumps({'outcomes':[]}))
    args=parser.parse_args(['learn','sync-outcomes','--input',str(dump),'--json'])
    assert args.func(args)==0
    assert json.loads(capsys.readouterr().out)['dry_run'] is True


def test_watchers_and_doc_approval_propose_without_applying(tmp_path):
    from spritewarden.learning.project_watch import register_project
    from spritewarden.learning.registry import register_dependencies
    from spritewarden.learning.models import Proposal, DocEvent
    from spritewarden.learning.proposals import review
    db=LearningDB(tmp_path/'learn.db')
    for name in ['spritewarden.json','pipeline.json','AGENTS.md']:
        (tmp_path/name).write_text('{}')
    (tmp_path/'docs').mkdir(); (tmp_path/'docs'/'sprite-warden-issues.md').write_text('retraction')
    assert register_project(db,'project',tmp_path)==4
    assert register_dependencies(db,'provider',operation_docs=['https://example.test/edit'],skill_paths=[tmp_path/'AGENTS.md'])==2
    eid=db.insert_event(DocEvent(source_id='operation-doc',provider='provider',diff_text='- size\n+ no size'))
    pid=db.insert_proposal(Proposal(kind='review_doc_change',target='event',what='changed',how='review',why='drift',evidence={'event_id':eid}))
    review(db,pid,True)
    follow=[p for p in db.list_proposals() if p.kind=='change_param']
    assert len(follow)==1 and follow[0].status=='open'
    assert follow[0].evidence['automatic_application'] is False
    review(db,pid,True)
    assert len([p for p in db.list_proposals() if p.kind=='change_param'])==1


def test_outcome_sync_and_corrections_are_reviewable_idempotent(tmp_path):
    from spritewarden.learning.tipshare_bridge import sync_outcomes, import_corrections, curated_corrections
    db=LearningDB(tmp_path/'learn.db')
    payload={'tips':[{'uuid':'tip-u','id':34}], 'outcomes':[{'uuid':'out-u','tip_uuid':'tip-u','revision':1,'worked':False,'note':'qualified','author':'owner'}]}
    assert sync_outcomes(db,payload,dry_run=True)['changed']==1
    assert not db.list_proposals()
    assert sync_outcomes(db,payload,dry_run=False)['changed']==1
    assert sync_outcomes(db,payload,dry_run=False)['changed']==0
    payload['outcomes'][0].update(revision=2,note='retracted')
    assert sync_outcomes(db,payload,dry_run=False)['changed']==1
    assert len(db.list_proposals())==2
    proposals=curated_corrections()
    assert {p['tip_id'] for p in proposals} == {34,31,40,60,63}
    assert next(p for p in proposals if p['tip_id']==60)['evidence_state']=='causal_hypothesis_unproven'
    result=import_corrections(db,proposals,dry_run=True)
    assert result['changed']==5 and len(db.list_proposals())==2
    import_corrections(db,proposals,dry_run=False)
    assert import_corrections(db,proposals,dry_run=False)['changed']==0
    assert len(db.list_proposals())==7


def test_recipe_versions_and_verified_uuid_receipts(tmp_path):
    from spritewarden.learning.recipe_tips import build_recipe_tips, publish_tips, mark_exported
    registry={'version':1,'pipeline_rules':['one anchor'],'stages':{'views':'review'},'providers':{'demo':{'identity':{'entity':True},'operations':{}}}}
    first=build_recipe_tips(registry)[0]
    registry['providers']['demo']['identity']['entity']=False
    second=build_recipe_tips(registry)[0]
    assert first['export_key'] != second['export_key']
    assert 'one anchor' in first['solution'] and 'identity' in first['solution'].lower()
    db=LearningDB(tmp_path/'learn.db')
    with pytest.raises(ValueError): mark_exported([first],db)
    calls=[]
    class Session:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def call_tool(self,name,args):
            calls.append((name,args))
            if name=='search': return []
            if name=='share_tip': return {'id':1,'uuid':'a1111111-1111-4111-8111-111111111111'}
            return dict(args,id=1,uuid='a1111111-1111-4111-8111-111111111111',title=first['title'],solution=first['solution'])
    result=publish_tips([first],db,command=['fake'],dry_run=False,session_factory=lambda _:Session())
    assert result[0]['action']=='published'
    assert any(n=='get_tip' for n,a in calls)
    preview=publish_tips([second],db,dry_run=True)
    assert preview[0]['supersedes']=='a1111111-1111-4111-8111-111111111111'


def test_failed_fetch_preserves_baseline_and_reports_capture(tmp_path):
    from spritewarden.learning.doctor import report
    db=LearningDB(tmp_path/'learn.db')
    s=Source(id='docs',provider='p',kind='md',url='https://example.test/docs')
    db.upsert_source(s)
    check_source(s,db,fetch_fn=lambda *a,**k: FetchResult(s.url,200,'good'))
    s=db.get_source(s.id)
    assert check_source(s,db,fetch_fn=lambda *a,**k: FetchResult(s.url,503,'bad')) == []
    assert db.latest_snapshot(s.id).body == 'good'
    health=report(db)
    assert health['sources']['failed'] == 1
    assert any('zero' in w for w in health['warnings'])
    check_source(db.get_source(s.id),db,fetch_fn=lambda *a,**k: FetchResult(s.url,304,not_modified=True))
    assert report(db)['sources']['failed'] == 0
