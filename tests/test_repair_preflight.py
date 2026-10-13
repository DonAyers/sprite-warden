import pytest
from PIL import Image
from spritewarden.repair_preflight import inspect_plan


def test_candidate_must_change_required_joint_region(tmp_path):
    source=tmp_path/'north.png';candidate=tmp_path/'candidate.png'
    image=Image.new('RGBA',(16,16),'gray');image.save(source)
    image.putpixel((12,12),(30,30,30,255));image.save(candidate)
    result=inspect_plan(tmp_path,{'source':'north.png','candidate':'candidate.png','goal':'redraw elbow',
        'defect_kind':'orientation','proposed_action':'geometry_patch','required_regions':{'elbow':[0,0,4,4]}})
    assert result['status']=='blocked'
    assert result['metrics']['changed_pixels']==1
    assert result['metrics']['required_region_changes']['elbow']==0


def test_unchanged_paid_request_after_noop_is_blocked(tmp_path):
    Image.new('RGBA',(16,16),'gray').save(tmp_path/'north.png')
    result=inspect_plan(tmp_path,{'source':'north.png','goal':'back arm','defect_kind':'orientation',
        'proposed_action':'inpaint','available_actions':['inpaint'], 'spend_cap_usd':1.0,'quoted_costs':{'inpaint':0.5},
        'request_fingerprint':'a'*64,'attempts':[{'action':'inpaint','request_fingerprint':'a'*64,'changed_pixels':0}]})
    assert result['status']=='blocked'
    assert 'inpaint' not in result['eligible_actions']
    assert 'no-op' in result['excluded_actions']['inpaint']


def test_jev_is_optional_authorized_and_advisory(tmp_path,monkeypatch):
    from spritewarden.router.jev import DecisionOutcome, JevClient
    from spritewarden import events
    Image.new('RGBA',(16,16),'gray').save(tmp_path/'north.png')
    calls=[]
    monkeypatch.setattr(JevClient,'is_available',lambda self:True)
    monkeypatch.setattr(JevClient,'ask_decision',lambda self,**kwargs:(calls.append(kwargs) or DecisionOutcome(question_id='repair_strategy',decision='geometry_patch',confidence=0.9)))
    monkeypatch.setattr(events,'emit',lambda *args,**kwargs:'intent')
    request={'source':'north.png','goal':'back arm','defect_kind':'orientation','proposed_action':'geometry_patch'}
    assert inspect_plan(tmp_path,request,ask_jev=True)['decision_status']=='authorization_required'
    assert calls==[]
    result=inspect_plan(tmp_path,request,ask_jev=True,authorize_decision_spend=True)
    assert result['decision']=='geometry_patch'
    assert result['execution_authorized'] is False and result['model_calls']==1
    assert len(calls)==1
    assert calls[0]['options'].keys()=={'geometry_patch','ask_owner'}


@pytest.mark.parametrize('extra',[
    {'source':'../outside.png'}, {'required_regions':{'elbow':[0,0,17,4]}},
    {'required_regions':{'elbow':[False,0,4,4]}}, {'spend_cap_usd':True},
    {'spend_cap_usd':float('nan')}, {'proposed_action':'accept'},
    {'unknown_command':'execute'}, {'request_fingerprint':'BAD'},
])
def test_invalid_request_fails_closed(tmp_path,extra):
    Image.new('RGBA',(16,16),'gray').save(tmp_path/'north.png')
    request={'source':'north.png','goal':'back arm','defect_kind':'orientation','proposed_action':'geometry_patch',**extra}
    with pytest.raises(ValueError):inspect_plan(tmp_path,request)


def test_noop_candidate_and_mask_metrics_are_measured(tmp_path):
    Image.new('RGBA',(16,16),(0,0,0,0)).save(tmp_path/'north.png')
    Image.new('RGBA',(16,16),'white').save(tmp_path/'mask.png')
    result=inspect_plan(tmp_path,{'source':'north.png','candidate':'north.png','mask':'mask.png',
        'goal':'back arm','defect_kind':'orientation','proposed_action':'geometry_patch'})
    assert result['status']=='blocked'
    assert result['metrics']['mask_pixels']==256 and result['metrics']['opaque_source_pixels_in_mask']==0
    assert any('intentional addition' in w for w in result['warnings'])


@pytest.mark.parametrize('quote,cap',[(None,1.0),(1.0,0.5)])
def test_missing_or_over_cap_quote_excludes_generation(tmp_path,quote,cap):
    Image.new('RGBA',(16,16),'gray').save(tmp_path/'north.png')
    result=inspect_plan(tmp_path,{'source':'north.png','goal':'back arm','defect_kind':'orientation',
        'proposed_action':'regenerate_direction','available_actions':['regenerate_direction'],
        'spend_cap_usd':cap,'quoted_costs':{} if quote is None else {'regenerate_direction':quote}})
    assert result['status']=='blocked'
    assert result['eligible_actions']==['ask_owner']


def test_invalid_jev_selection_cannot_reintroduce_excluded_action(tmp_path,monkeypatch):
    from spritewarden.router.jev import DecisionOutcome,JevClient
    from spritewarden import events
    Image.new('RGBA',(16,16),'gray').save(tmp_path/'north.png')
    monkeypatch.setattr(JevClient,'is_available',lambda self:True)
    monkeypatch.setattr(JevClient,'ask_decision',lambda self,**kwargs:DecisionOutcome(question_id='repair_strategy',decision='accept',confidence=1))
    monkeypatch.setattr(events,'emit',lambda *a,**k:'intent')
    result=inspect_plan(tmp_path,{'source':'north.png','goal':'back arm','defect_kind':'orientation','proposed_action':'geometry_patch'},ask_jev=True,authorize_decision_spend=True)
    assert result['decision_status']=='unknown' and result['decision'] is None and not result['execution_authorized']


def test_intent_failure_prevents_model_call(tmp_path,monkeypatch):
    from spritewarden.router.jev import JevClient
    from spritewarden import events
    Image.new('RGBA',(16,16),'gray').save(tmp_path/'north.png')
    monkeypatch.setattr(JevClient,'is_available',lambda self:True)
    monkeypatch.setattr(JevClient,'ask_decision',lambda *a,**k:pytest.fail('must not call model'))
    monkeypatch.setattr(events,'emit',lambda *a,**k:None)
    result=inspect_plan(tmp_path,{'source':'north.png','goal':'back arm','defect_kind':'orientation','proposed_action':'geometry_patch'},ask_jev=True,authorize_decision_spend=True)
    assert result['decision_status']=='intent_capture_failed' and result['model_calls']==0


def test_cli_report_and_mcp_are_read_only(tmp_path,capsys):
    import json
    from spritewarden.repair_preflight_cli import main
    from spritewarden.server.mcp_server import preflight_repair
    Image.new('RGBA',(16,16),'gray').save(tmp_path/'north.png')
    request={'source':'north.png','goal':'<script>alert(1)</script>','defect_kind':'orientation','proposed_action':'geometry_patch'}
    plan=tmp_path/'plan.json';plan.write_text(json.dumps(request))
    report=tmp_path/'report.html'
    original=(tmp_path/'north.png').read_bytes()
    assert main(['--project',str(tmp_path),'--plan',str(plan),'--html',str(report)])==0
    result=json.loads(capsys.readouterr().out)
    assert result['model_calls']==0 and not result['execution_authorized']
    assert '<script>' not in report.read_text() and '&lt;script&gt;' in report.read_text()
    assert 'data:image/png;base64,' in report.read_text()
    assert preflight_repair(str(tmp_path),request)['status']=='proposal'
    assert (tmp_path/'north.png').read_bytes()==original
    assert set(p.name for p in tmp_path.iterdir())=={'north.png','plan.json','report.html'}


def test_orientation_plan_cannot_freeze_required_elbow(tmp_path):
    image=tmp_path/'north.png';Image.new('RGBA',(16,16),'gray').save(image)
    result=inspect_plan(tmp_path,{'source':'north.png','goal':'elbow and cannon facing away',
        'defect_kind':'orientation','proposed_action':'color_patch','required_regions':{'elbow':[0,0,4,4]},
        'preserved_regions':['elbow'],'available_actions':['color_patch','geometry_patch','regenerate_direction']})
    assert result['status']=='blocked'
    assert any('elbow' in b for b in result['blockers'])
    assert 'color_patch' not in result['eligible_actions']
    assert result['execution_authorized'] is False
    assert result['model_calls']==0


def test_completion_receipt_failure_preserves_advice_without_retry(tmp_path,monkeypatch):
    from spritewarden.router.jev import DecisionOutcome,JevClient
    from spritewarden import events
    Image.new('RGBA',(16,16),'gray').save(tmp_path/'north.png')
    calls=[];emitted=[]
    monkeypatch.setattr(JevClient,'is_available',lambda self:True)
    outcome=DecisionOutcome(question_id='repair_strategy',decision='geometry_patch',confidence=0.9,cost_usd=0.001)
    monkeypatch.setattr(JevClient,'ask_decision',lambda self,**kwargs:(calls.append(kwargs) or outcome))
    def emit(name,**kwargs):
        emitted.append(name)
        return 'intent' if name=='repair.decision_started' else None
    monkeypatch.setattr(events,'emit',emit)
    result=inspect_plan(tmp_path,{'source':'north.png','goal':'back arm','defect_kind':'orientation',
        'proposed_action':'geometry_patch'},ask_jev=True,authorize_decision_spend=True)
    assert result['decision_status']=='recommended'
    assert result['decision']=='geometry_patch' and result['confidence']==0.9
    assert result['decision_receipt']['cost_usd']==0.001
    assert result['model_calls']==1 and len(calls)==1
    assert emitted==['repair.decision_started','repair.decision_completed']
    assert result['completion_receipt_status']=='capture_failed'
    assert any('completion' in warning.lower() and 'failed' in warning.lower() for warning in result['warnings'])


def test_blocked_plan_skips_explicitly_authorized_decision(tmp_path,monkeypatch):
    from spritewarden.router.jev import JevClient
    Image.new('RGBA',(16,16),'gray').save(tmp_path/'north.png')
    monkeypatch.setattr(JevClient,'ask_decision',lambda *a,**k:pytest.fail('blocked plan must not call model'))
    result=inspect_plan(tmp_path,{'source':'north.png','candidate':'north.png','goal':'back arm',
        'defect_kind':'orientation','proposed_action':'geometry_patch'},ask_jev=True,authorize_decision_spend=True)
    assert result['status']=='blocked'
    assert result['decision_status']=='skipped_blocked'
    assert result['model_calls']==0
