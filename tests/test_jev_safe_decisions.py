import pytest
from spritewarden.router.jev import JevClient


def mock_reply(monkeypatch,reply):
    import spritewarden.router.jev as module
    monkeypatch.delenv('SPRITEWARDEN_OFFLINE',raising=False)
    class Response:
        status_code=200
        def json(self):return reply
    class Client:
        def __init__(self,**kwargs):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def post(self,*args,**kwargs):return Response()
    monkeypatch.setattr(module.httpx,'Client',Client)


def test_missing_answer_is_unknown_not_approval(monkeypatch):
    mock_reply(monkeypatch,{'answers':{},'usage':{'cost':0.001}})
    result=JevClient(api_key='TEST').ask_decision({},'Proceed?',primitive='noul')
    assert result.decision is None
    assert result.confidence is None
    assert result.cost_usd==0.001
    assert result.error


def test_offline_orchestration_is_review_not_production_approval(monkeypatch):
    from PIL import Image
    from spritewarden.judge.vision import VisionJudge
    from spritewarden.judge import evaluate_still
    monkeypatch.setenv('SPRITEWARDEN_OFFLINE','1')
    image=Image.new('RGBA',(16,16),(89,112,128,255))
    result=VisionJudge(offline=True).orchestrate_judgment(evaluate_still(image,'luap-40'),image)
    assert not result.passed
    assert result.production_ready_prob==0
    assert result.recommended_action=='review'
    assert result.decision_status=='unknown'


@pytest.mark.parametrize('primitive,answer',[
    ('choice',{'choice':'execute_unlisted','confidence':1}),
    ('choice',{'choice':True}),
    ('noul',{'noul':True}),
    ('noul',{'noul':1.5}),
    ('choice',{'choice':'stop','confidence':float('nan')}),
])
def test_invalid_typed_answer_is_unknown(primitive,answer,monkeypatch):
    mock_reply(monkeypatch,{'answers':{'q1':answer},'usage':{'cost':0.001}})
    result=JevClient(api_key='TEST').ask_decision({},'Proceed?',primitive=primitive,options=['stop'])
    assert result.decision is None and result.error
    assert result.confidence is None


def test_offline_does_not_manufacture_approval_or_confidence(monkeypatch):
    monkeypatch.setenv('SPRITEWARDEN_OFFLINE','1')
    result=JevClient(api_key='TEST').ask_decision({},'Proceed?',primitive='choice',options=['accept','stop'])
    assert result.decision is None and result.confidence is None
    assert result.probabilities=={} and result.used_fallback


@pytest.mark.parametrize('action',[
    {'error':'missing or invalid typed decision answer'},
    {'decision':'accept','used_fallback':True},
    {'decision':'inpaint_part'},
    {'decision':'accept'},
])
def test_production_approval_requires_valid_accept_action(monkeypatch,action):
    from spritewarden.judge.models import StillScorecard
    from PIL import Image
    from spritewarden.judge.vision import VisionJudge
    from spritewarden.router.jev import DecisionOutcome
    judge=VisionJudge(offline=True)
    outcomes=iter([
        DecisionOutcome(question_id='production_ready',decision=0.9),
        DecisionOutcome(question_id='remediation_action',**action),
    ])
    monkeypatch.setattr(judge.jev,'ask_decision',lambda **kwargs:next(outcomes))
    card=StillScorecard(path='synthetic.png',passed=True,score=1.0,gates=[],summary='Synthetic passing gates')
    result=judge.orchestrate_judgment(card,Image.new('RGBA',(16,16),'gray'))
    known=not action.get('error') and not action.get('used_fallback')
    approved=known and action.get('decision')=='accept'
    assert result.production_ready_prob==0.9
    assert result.passed is approved
    assert result.decision_status==('assessed' if known else 'unknown')
    assert result.recommended_action==(action['decision'] if known else 'review')
    assert ('approved' in result.reasoning_summary.lower()) is approved
