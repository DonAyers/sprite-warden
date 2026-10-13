from spritewarden.judge.vision import reference_prompt


import pytest


@pytest.mark.parametrize('facing,back', [('south','hidden'),('east','edge-on or occluded'),('west','edge-on or occluded'),('north_west','partially visible'),('north','visible')])
def test_prompt_distinguishes_visible_and_hidden_surfaces(facing,back):
    prompt=reference_prompt(candidate_facing=facing,faces={'back':'four bolts'})
    assert f'BACK: {back}' in prompt
    assert 'far corners may be occluded' in prompt
    assert 'not a geometric visibility measurement' in prompt


def test_broken_json_retains_cost_without_retry(monkeypatch):
    from spritewarden.judge.vision import VisionJudge
    import spritewarden.judge.vision as module
    calls=[]
    class Response:
        status_code=200
        def json(self):return {'choices':[{'message':{'content':'{"notes":"unterminated'}}], 'usage':{'cost':0.05}}
    class Client:
        def __init__(self,**kwargs):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def post(self,*args,**kwargs):calls.append(kwargs['json']);return Response()
    monkeypatch.setattr(module.httpx,'Client',Client)
    parsed,cost,error=VisionJudge(offline=True)._chat_json([{'type':'text','text':'test'}])
    assert parsed is None and len(calls)==1
    assert cost==0.05
    assert error.startswith('invalid_json:')
    assert calls[0]['temperature']==0
    assert calls[0]['top_p']==1
    assert 'seed' not in calls[0]  # Provider support is not assumed; cache, not sampling, gives replay stability.


def test_assessment_error_is_not_serialized_as_artistic_failure():
    from spritewarden.judge.vision import ReferenceComparison,reference_gate
    from spritewarden.gates.records import gates_from_scorecard
    gate=reference_gate(ReferenceComparison(status='error',notes='invalid_json: truncated',cost_usd=0.05))
    record=gates_from_scorecard(gate)['vision_reference']
    assert record['status']=='error'
    assert record['metrics']['assessment']=='not_assessed'
    assert record['metrics']['cost_usd']==0.05
    assert not record['passed']


def test_cache_audits_effective_request_and_ignores_face_metadata(tmp_path,monkeypatch):
    from PIL import Image
    from spritewarden.judge.vision import VisionJudge
    monkeypatch.setenv('SPRITEWARDEN_VISION_CACHE',str(tmp_path))
    judge=VisionJudge(offline=True);monkeypatch.setattr(judge,'is_available',lambda:True)
    calls=[]
    def chat(content):
        calls.append(content)
        return {'same_character':True,'invented_parts':[],'missing_visible_parts':[]},0.01,None
    monkeypatch.setattr(judge,'_chat_json',chat)
    image=Image.new('RGBA',(8,8),'red')
    first=judge.compare_to_reference(image,image,facing='north',faces={'back':'four bolts','_about':'old notes'})
    second=judge.compare_to_reference(image,image,facing='north',faces={'back':'four bolts','_about':'changed notes'})
    assert len(calls)==1
    assert first.request_audit['sampling']=={'temperature':0,'top_p':1}
    import hashlib
    assert first.request_audit['prompt_sha256']==hashlib.sha256(calls[0][0]['text'].encode()).hexdigest()
    assert second.request_fingerprint==first.request_fingerprint


def test_error_attempt_is_audited_but_never_cached_as_completed(tmp_path,monkeypatch):
    import json
    from PIL import Image
    from spritewarden.judge.vision import VisionJudge,reference_gate
    monkeypatch.setenv('SPRITEWARDEN_VISION_CACHE',str(tmp_path))
    judge=VisionJudge(offline=True);monkeypatch.setattr(judge,'is_available',lambda:True)
    monkeypatch.setattr(judge,'_chat_json',lambda content:(None,0.05,'invalid_json: truncated'))
    image=Image.new('RGBA',(8,8),'red')
    result=judge.compare_to_reference(image,image,facing='west')
    history=list(tmp_path.glob('*/history/*.json'))
    assert len(history)==1
    assert not list(tmp_path.glob('*/default.json'))
    assert json.loads(history[0].read_text())['result']['status']=='error'
    assert reference_gate(result).metrics['request']['params']['facing']=='west'


def test_set_summary_separates_mismatch_from_unassessed_error():
    from spritewarden.judge.vision import ReferenceComparison,reference_gate
    from spritewarden.judge.views import vision_summary_gate
    summary=vision_summary_gate({'east':reference_gate(ReferenceComparison(status='mismatch',notes='extra bolt')),
                                'west':reference_gate(ReferenceComparison(status='error',notes='invalid_json'))})
    assert summary.metrics['mismatch_views']==['east']
    assert summary.metrics['unassessed_views']==['west']
    assert summary.metrics['status']=='error'
    assert 'unassessed' in summary.summary
    assert not summary.passed


def test_mcp_forwards_faces_and_pipeline_description_offline(tmp_path,monkeypatch):
    import json
    from types import SimpleNamespace
    from PIL import Image
    from spritewarden.server import mcp_server as server
    from spritewarden.judge.vision import parse_reference_response
    folder=tmp_path/'views';folder.mkdir()
    for facing in ['south','south-east','east','north-east','north','north-west','west','south-west']:
        Image.new('RGBA',(16,16),(120,120,120,255)).save(folder/(facing+'.png'))
    (tmp_path/'pipeline.json').write_text(json.dumps({'description':'PIPELINE description','faces':{'back':'four bolts'}}))
    identity=SimpleNamespace(prompt_description='IDENTITY description',turnaround_stills={},features={},side_front_area_ratio=None,palette_id=None,model_dump=lambda:{'id':'TEST'})
    monkeypatch.setattr(server,'get_catalog',lambda:SimpleNamespace(get_identity=lambda key:identity))
    calls=[]
    class Judge:
        def __init__(self,**kwargs):pass
        def compare_to_reference(self,*args,**kwargs):
            calls.append(kwargs)
            return parse_reference_response({'same_character':True,'invented_parts':[],'missing_visible_parts':[]},facing=kwargs['facing'])
    monkeypatch.setattr(server,'VisionJudge',Judge)
    server.judge_direction_set(str(folder),character_id='TEST',palette=None,vision=True,write_gate_record=False)
    assert len(calls)==7
    assert all(call['faces']=={'back':'four bolts'} for call in calls)
    assert all(call['description']=='PIPELINE description' for call in calls)
    assert not list(folder.glob('*.gate.json'))


def test_size_tier_is_content_height_not_canvas_size():
    from types import SimpleNamespace
    from PIL import Image,ImageDraw
    from spritewarden.judge.start_image import check_start_image
    image=Image.new('RGBA',(96,96));ImageDraw.Draw(image).rectangle((20,18,75,76),fill=(120,120,120,255))
    identity=SimpleNamespace(size_tier='96',dimensions=(96,96),features={},palette_id=None)
    wrong=check_start_image(image,identity=identity,palette=None)
    explicit=check_start_image(image,identity=identity,palette=None,size_tier='48-64')
    assert wrong.metrics['content_height']==59
    assert not wrong.details['checks']['content_height']['passed']
    assert explicit.details['checks']['content_height']['passed']
    assert image.size==(96,96)


@pytest.mark.parametrize('reply',[{}, {'same_character':True,'invented_parts':[]}])
def test_incomplete_json_object_is_unassessed_not_match(reply,tmp_path,monkeypatch):
    from PIL import Image
    from spritewarden.judge.vision import VisionJudge
    monkeypatch.setenv('SPRITEWARDEN_VISION_CACHE',str(tmp_path))
    judge=VisionJudge(offline=True);monkeypatch.setattr(judge,'is_available',lambda:True)
    monkeypatch.setattr(judge,'_chat_json',lambda content:(reply,0.01,None))
    image=Image.new('RGBA',(8,8),'red')
    result=judge.compare_to_reference(image,image)
    assert result.status=='error'
    assert 'invalid_schema' in result.notes
    assert not list(tmp_path.glob('*/default.json'))


def test_malformed_finding_entries_are_unassessed_and_never_reused(tmp_path,monkeypatch):
    import json
    from PIL import Image
    from spritewarden.judge.vision import VisionJudge,reference_gate
    monkeypatch.setenv('SPRITEWARDEN_VISION_CACHE',str(tmp_path))
    judge=VisionJudge(offline=True);monkeypatch.setattr(judge,'is_available',lambda:True)
    calls=[]
    def chat(content):
        calls.append(content)
        return {'same_character':True,'invented_parts':[None,17,{}],'missing_visible_parts':[False]},0.01,None
    monkeypatch.setattr(judge,'_chat_json',chat)
    image=Image.new('RGBA',(8,8),'red')
    first=judge._compare_to_reference(image,image)
    assert first.status=='error'
    assert first.cost_usd==0.01
    assert 'invalid_schema' in first.notes
    for _ in range(2):
        result=judge.compare_to_reference(image,image)
        assert result.status=='error'
        assert result.cost_usd==0.01
        assert not reference_gate(result).passed
        assert not list(tmp_path.glob('*/default.json'))
    assert len(calls)==3
    history=list(tmp_path.glob('*/history/*.json'))
    assert len(history)==2
    assert all(json.loads(path.read_text())['result']['status']=='error' for path in history)


@pytest.mark.parametrize('field',['invented_parts','missing_visible_parts','changed_parts','expected_hidden_parts'])
@pytest.mark.parametrize('finding',[None,17,False,{},'', '  ',{'location':'head'}, {'unknown':'bolt'}, {'part':'  ','detail':'  '}])
def test_each_finding_list_rejects_malformed_or_empty_entries(field,finding,monkeypatch):
    from PIL import Image
    from spritewarden.judge.vision import VisionJudge
    judge=VisionJudge(offline=True);monkeypatch.setattr(judge,'is_available',lambda:True)
    reply={'same_character':True,'invented_parts':[],'missing_visible_parts':[],field:[finding]}
    monkeypatch.setattr(judge,'_chat_json',lambda content:(reply,0.01,None))
    image=Image.new('RGBA',(8,8),'red')
    result=judge._compare_to_reference(image,image)
    assert result.status=='error'
    assert result.cost_usd==0.01
    assert 'invalid_schema' in result.notes


@pytest.mark.parametrize('field',['changed_parts','expected_hidden_parts'])
@pytest.mark.parametrize('findings',[None,False,{},'bolt'])
def test_optional_finding_lists_require_lists_when_present(field,findings,monkeypatch):
    from PIL import Image
    from spritewarden.judge.vision import VisionJudge
    judge=VisionJudge(offline=True);monkeypatch.setattr(judge,'is_available',lambda:True)
    reply={'same_character':True,'invented_parts':[],'missing_visible_parts':[],field:findings}
    monkeypatch.setattr(judge,'_chat_json',lambda content:(reply,0.01,None))
    image=Image.new('RGBA',(8,8),'red')
    result=judge._compare_to_reference(image,image)
    assert result.status=='error'
    assert result.cost_usd==0.01
    assert 'invalid_schema' in result.notes


@pytest.mark.parametrize('field',['invented_parts','missing_visible_parts','changed_parts','expected_hidden_parts'])
@pytest.mark.parametrize('finding',[
    ' bolt ', {'part':'bolt','location':'head','size':'small','detail':'extra'},
    {'name':'bolt','where':'head','size_estimate':'small','change':'extra'},
    {'element':'bolt','reason':'extra'}, {'why':'extra'},
])
def test_valid_finding_strings_and_dict_aliases_are_preserved(field,finding,monkeypatch):
    from PIL import Image
    from spritewarden.judge.vision import VisionJudge,_part_notes
    judge=VisionJudge(offline=True);monkeypatch.setattr(judge,'is_available',lambda:True)
    reply={'same_character':True,'invented_parts':[],'missing_visible_parts':[],field:[finding]}
    monkeypatch.setattr(judge,'_chat_json',lambda content:(reply,0.01,None))
    image=Image.new('RGBA',(8,8),'red')
    result=judge._compare_to_reference(image,image)
    assert result.status==('mismatch' if field in ('invented_parts','missing_visible_parts') else 'match')
    assert getattr(result,field)==_part_notes([finding])
    assert result.cost_usd==0.01


def test_unseen_face_design_is_authoritative_not_anchor_only():
    prompt = reference_prompt(candidate_facing='north', faces={'back':'four bolts and two panel lines', '_about':'OLD FORBIDDEN TEXT: no bolts'})
    assert 'OLD FORBIDDEN TEXT' not in prompt
    assert 'takes precedence over absence from the anchor' in prompt
    assert "A round boss or bolt on the back or the sides of the" not in prompt
    assert 'four bolts and two panel lines' in prompt
