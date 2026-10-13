import json
from PIL import Image
import pytest
from spritewarden.enforce.core import check_call
from spritewarden.gates.records import write_record


def setup_project(root):
    from uuid import UUID
    cid='11111111-1111-4111-8111-111111111111'
    folder=root/'views';folder.mkdir()
    for facing in ('south','east'):
        p=folder/(facing+'.png');Image.new('RGBA',(8,8),'red').save(p)
    manifest=root/'manifest.json';manifest.write_text(json.dumps({'pixellab_characters':[{'character_id':cid,'dir':'views'}]}))
    (root/'spritewarden.json').write_text(json.dumps({'manifest':'manifest.json'}))
    source=root/'fixed.png';Image.new('RGBA',(8,8),'red').save(source)
    write_record(source, {'direction_set':True,'views':True,'vision_reference':{'passed':True,'metrics':{'status':'match'}}}, params={'command':'judge-views','facing':'east'})
    return source,cid


def test_character_readback_must_match_all_registered_views(tmp_path):
    import shutil
    from spritewarden.publication import prepare,confirm,verify_character,character_sync_reasons
    from spritewarden.judge.views import GAME_ORDER
    source,cid=setup_project(tmp_path)
    folder=tmp_path/'views'
    for facing in GAME_ORDER:
        p=folder/(facing.replace('_','-')+'.png')
        if not p.exists():Image.new('RGBA',(8,8),'red').save(p)
    for facing in GAME_ORDER:
        p=folder/(facing.replace('_','-')+'.png')
        write_record(p,{'direction_set':True,'views':True,'vision_reference':{'passed':True,'metrics':{'status':'match'}}},params={'command':'judge-views','facing':facing})
    # Re-evidence the separate publication candidate after directory changes.
    write_record(source, {'direction_set':True,'views':True,'vision_reference':{'passed':True,'metrics':{'status':'match'}}},params={'command':'judge-views','facing':'east'})
    args={'image':'upload:22222222-2222-4222-8222-222222222222','target':f'character:{cid}:east'}
    receipt=prepare(tmp_path,source,args['image'],args['target'],uploaded_file=source,actor='TEST',reason='Publish')
    assert character_sync_reasons(tmp_path,cid)
    animation={'character_id':cid,'mode':'pixminimax','frame_count':16,'directions':['east'],'action_description':'fires laser'}
    verdict=check_call('mcp__pixellab__animate_character',animation,tmp_path)
    assert verdict.decision=='deny' and any(r.startswith('remote character sync:') for r in verdict.reasons)
    confirm(tmp_path,receipt['id'],source)
    downloaded=tmp_path/'remote-download';shutil.copytree(folder,downloaded)
    from spritewarden.publication_cli import main
    assert main(['--project',str(tmp_path),'verify-character',cid,'--download-dir',str(downloaded)])==0
    assert not character_sync_reasons(tmp_path,cid)
    assert check_call('mcp__pixellab__animate_character',animation,tmp_path).decision=='allow'
    Image.new('RGBA',(8,8),'blue').save(downloaded/'north.png')
    with pytest.raises(ValueError,match='north'):
        verify_character(tmp_path,cid,downloaded)
    assert character_sync_reasons(tmp_path,cid)
    override=tmp_path/'tmp/generator_hook_override.json';override.parent.mkdir();override.write_text(json.dumps({'calls':1,'reason':'TEST override'}))
    assert check_call('mcp__pixellab__animate_character',animation,tmp_path).decision=='deny'
    assert json.loads(override.read_text())['calls']==1


def test_cancel_revokes_publication_without_remote_changes(tmp_path):
    from spritewarden.publication import prepare
    from spritewarden.publication_cli import main
    source,cid=setup_project(tmp_path)
    args={'image':'upload:22222222-2222-4222-8222-222222222222','target':f'character:{cid}:east'}
    receipt=prepare(tmp_path,source,args['image'],args['target'],uploaded_file=source,actor='TEST',reason='Publish')
    assert main(['--project',str(tmp_path),'cancel',receipt['id'],'--by','TEST','--reason','Inspect first'])==0
    assert check_call('mcp__pixellab__save_to_asset',args,tmp_path).decision=='deny'


def test_local_review_board_requires_no_model(tmp_path,capsys):
    from spritewarden.publication_cli import main
    source,cid=setup_project(tmp_path)
    output=tmp_path/'review.png'
    assert main(['--project',str(tmp_path),'review','--views','views','--out','review.png'])==0
    assert output.is_file()
    assert (tmp_path/'review.png.json').is_file()


def test_publish_cli_dispatch(tmp_path, capsys):
    from spritewarden.cli import main
    with pytest.raises(SystemExit) as result:
        main(['publish','--help'])
    assert result.value.code == 0
    assert 'readback' in capsys.readouterr().out


def test_publication_requires_exact_source_target_and_human_receipt(tmp_path):
    from spritewarden.publication import prepare
    source,cid=setup_project(tmp_path)
    args={'image':'upload:22222222-2222-4222-8222-222222222222','target':f'character:{cid}:east'}
    override=tmp_path/'tmp/generator_hook_override.json';override.parent.mkdir();override.write_text(json.dumps({'calls':1,'reason':'TEST override must not bypass publication'}))
    assert check_call('mcp__pixellab__save_to_asset',args,tmp_path).decision=='deny'
    assert json.loads(override.read_text())['calls']==1
    receipt=prepare(tmp_path,source,args['image'],args['target'],uploaded_file=source,actor='TEST owner',reason='Replace corrected arm view')
    assert receipt['operation']=='save_to_asset'
    assert check_call('mcp__pixellab__save_to_asset',args,tmp_path).decision=='allow'
    assert check_call('mcp__pixellab__save_to_asset',args|{'description':None},tmp_path).decision=='allow'
    assert check_call('mcp__pixellab__save_to_asset',args|{'target':f'character:{cid}:south'},tmp_path).decision=='deny'
    Image.new('RGBA',(8,8),'blue').save(source)
    assert check_call('mcp__pixellab__save_to_asset',args,tmp_path).decision=='deny'


def test_confirm_requires_matching_readback_and_closes_authorization(tmp_path):
    from spritewarden.publication import prepare, confirm
    source,cid=setup_project(tmp_path)
    args={'image':'upload:22222222-2222-4222-8222-222222222222','target':f'character:{cid}:east'}
    receipt=prepare(tmp_path,source,args['image'],args['target'],uploaded_file=source,actor='TEST',reason='Publish repaired view')
    downloads=tmp_path/'downloads';downloads.mkdir()
    wrong=downloads/'remote.png';Image.new('RGBA',(8,8),'blue').save(wrong)
    with pytest.raises(ValueError,match='readback'):
        confirm(tmp_path,receipt['id'],wrong)
    assert confirm(tmp_path,receipt['id'],source)['status']=='verified'
    assert check_call('mcp__pixellab__save_to_asset',args,tmp_path).decision=='deny'


def test_publication_does_not_waive_vision_and_checks_uploaded_bytes(tmp_path):
    from spritewarden.publication import prepare
    source,cid=setup_project(tmp_path)
    image='upload:22222222-2222-4222-8222-222222222222';target=f'character:{cid}:east'
    wrong=tmp_path/'wrong.png';Image.new('RGBA',(8,8),'blue').save(wrong)
    with pytest.raises(ValueError,match='uploaded'):
        prepare(tmp_path,source,image,target,uploaded_file=wrong,actor='TEST',reason='Fix')
    write_record(source, {'direction_set':True,'views':False,'vision_reference':{'passed':False,'status':'fail','metrics':{'status':'mismatch'}}},params={'command':'judge-views','facing':'east'})
    with pytest.raises(ValueError,match='ready'):
        prepare(tmp_path,source,image,target,uploaded_file=source,actor='TEST',reason='Fix')
