from PIL import Image
from spritewarden.judge.vision import VisionJudge


def test_identical_rejudgment_preserves_exact_acceptance_evidence(tmp_path):
    from spritewarden.gates.records import write_record, load_record
    from spritewarden.gates.acceptance import accept
    p=tmp_path/'east.png';Image.new('RGBA',(8,8),'red').save(p)
    gates={'direction_set':{'passed':True},'views':{'passed':False},'vision_reference':{'passed':False,'status':'fail','metrics':{'status':'mismatch'},'errors':['surface bolt']}}
    params={'command':'judge-views','facing':'east'}
    write_record(p,gates,params=params)
    accept([p],actor='TEST owner',reason='Intentional bolt')
    before=p.with_suffix('.png.gate.json').read_bytes()
    write_record(p,gates,params=params)
    assert p.with_suffix('.png.gate.json').read_bytes()==before


def test_failed_intent_prevents_vision_request(tmp_path, monkeypatch):
    import pytest
    from spritewarden import events
    monkeypatch.setenv('SPRITEWARDEN_VISION_CACHE',str(tmp_path))
    monkeypatch.setattr(events,'emit',lambda *a,**k:None)
    judge=VisionJudge(api_key='test-not-a-secret')
    monkeypatch.setattr(judge,'_chat_json',lambda *a:pytest.fail('should not call model'))
    with pytest.raises(RuntimeError,match='intent'):
        judge.compare_to_reference(Image.new('RGBA',(8,8),'red'),Image.new('RGBA',(8,8),'red'))


def test_refresh_cannot_overwrite_gate_records(tmp_path):
    import pytest
    from spritewarden.server.mcp_server import judge_direction_set
    with pytest.raises(ValueError, match='report-only'):
        judge_direction_set(str(tmp_path), vision=True, refresh_vision=True, write_gate_record=True)


def test_identical_reference_calls_reuse_result_and_refresh_preserves_default(tmp_path, monkeypatch):
    monkeypatch.setenv('SPRITEWARDEN_VISION_CACHE', str(tmp_path))
    judge = VisionJudge(api_key='test-not-a-secret')
    monkeypatch.setattr(judge, 'is_available', lambda: True)
    calls = []
    def chat(content):
        calls.append(content)
        return {'same_character': True, 'verdict': 'match', 'invented_parts': [], 'missing_visible_parts': []}, 0.01, None
    monkeypatch.setattr(judge, '_chat_json', chat)
    a = Image.new('RGBA', (8, 8), 'red')
    first = judge.compare_to_reference(a, a, facing='east')
    again = judge.compare_to_reference(a, a, facing='east')
    assert len(calls) == 1
    assert first.model_dump() == again.model_dump()
    judge.compare_to_reference(a, a, facing='east', refresh=True)
    assert len(calls) == 2
    judge.compare_to_reference(a, a, facing='east')
    assert len(calls) == 2
    a.putpixel((0, 0), (0,0,0,255))
    judge.compare_to_reference(a, a, facing='east')
    assert len(calls) == 3
    assert len(list(tmp_path.glob('*/history/*.json'))) == 3
