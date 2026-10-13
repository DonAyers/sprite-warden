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


def test_prepare_without_upload_readback_records_gap_and_still_requires_confirm(tmp_path):
    from spritewarden.publication import prepare,confirm,character_sync_reasons
    source,cid=setup_project(tmp_path)
    args={'image':'upload:22222222-2222-4222-8222-222222222222','target':f'character:{cid}:east'}
    receipt=prepare(tmp_path,source,args['image'],args['target'],provider_gap='pixellab: no upload download',actor='TEST',reason='Publish')
    assert receipt['uploaded_readback']=='unavailable'
    assert receipt['provider_gap']=='pixellab: no upload download'
    assert receipt['status']=='authorized'
    assert check_call('mcp__pixellab__save_to_asset',args,tmp_path).decision=='allow'
    assert character_sync_reasons(tmp_path,cid)
    animation={'character_id':cid,'mode':'pixminimax','frame_count':16,'directions':['east'],'action_description':'fires laser'}
    assert check_call('mcp__pixellab__animate_character',animation,tmp_path).decision=='deny'
    downloads=tmp_path/'downloads';downloads.mkdir()
    wrong=downloads/'wrong.png';Image.new('RGBA',(8,8),'blue').save(wrong)
    with pytest.raises(ValueError,match='readback'):
        confirm(tmp_path,receipt['id'],wrong)
    stored=tmp_path/'.spritewarden/publications'/(receipt['id']+'.json')
    assert json.loads(stored.read_text())['status']=='authorized'
    readback=downloads/'remote-readback.png';Image.new('RGBA',(8,8),'red').save(readback)
    assert confirm(tmp_path,receipt['id'],readback)['status']=='verified'
    assert check_call('mcp__pixellab__save_to_asset',args,tmp_path).decision=='deny'
    assert character_sync_reasons(tmp_path,cid)
    assert check_call('mcp__pixellab__animate_character',animation,tmp_path).decision=='deny'


def test_prepare_requires_exactly_one_readback_mode(tmp_path):
    from spritewarden.publication import prepare
    source,cid=setup_project(tmp_path)
    args=(tmp_path,source,'upload:22222222-2222-4222-8222-222222222222',f'character:{cid}:east')
    with pytest.raises(ValueError,match='exactly one'):
        prepare(*args,actor='TEST',reason='x')
    with pytest.raises(ValueError,match='exactly one'):
        prepare(*args,uploaded_file=source,provider_gap='pixellab: no download',actor='TEST',reason='x')
    assert not (tmp_path/'.spritewarden/publications').exists()


def test_prepare_cli_without_upload_readback(tmp_path,capsys):
    from spritewarden.publication_cli import main
    source,cid=setup_project(tmp_path)
    assert main(['--project',str(tmp_path),'prepare','--source',str(source),
        '--image','upload:22222222-2222-4222-8222-222222222222','--target',f'character:{cid}:east',
        '--no-upload-readback','pixellab: no upload download','--by','TEST','--reason','x'])==0
    result=json.loads(capsys.readouterr().out)
    assert result['uploaded_readback']=='unavailable' and result['status']=='authorized'


@pytest.mark.parametrize('gap',['','   ','x'*501,False,17,{}])
def test_prepare_rejects_invalid_provider_gap(tmp_path,gap):
    from spritewarden.publication import prepare
    source,cid=setup_project(tmp_path)
    with pytest.raises(ValueError,match='provider_gap'):
        prepare(tmp_path,source,'upload:22222222-2222-4222-8222-222222222222',f'character:{cid}:east',
                provider_gap=gap,actor='TEST',reason='x')
    assert not (tmp_path/'.spritewarden/publications').exists()


def test_prepare_upload_readback_mode_is_verified_but_not_publication_confirmed(tmp_path):
    from spritewarden.publication import prepare,character_sync_reasons
    source,cid=setup_project(tmp_path)
    receipt=prepare(tmp_path,source,'upload:22222222-2222-4222-8222-222222222222',f'character:{cid}:east',
                    uploaded_file=source,actor='TEST',reason='x')
    assert receipt['uploaded_readback']=='verified' and receipt['status']=='authorized'
    assert 'provider_gap' not in receipt
    assert character_sync_reasons(tmp_path,cid)


def test_gap_mode_keeps_complete_character_verification_mandatory(tmp_path):
    import shutil
    from spritewarden.publication import prepare,confirm,verify_character,character_sync_reasons
    source,cid=setup_project(tmp_path)
    for facing in ('north','west'):Image.new('RGBA',(8,8),'red').save(tmp_path/'views'/(facing+'.png'))
    for facing in ('north','east','south','west'):
        write_record(tmp_path/'views'/(facing+'.png'),{'direction_set':True,'views':True,
            'vision_reference':{'passed':True,'metrics':{'status':'match'}}},params={'command':'judge-views','facing':facing})
    write_record(source,{'direction_set':True,'views':True,'vision_reference':{'passed':True,'metrics':{'status':'match'}}},params={'command':'judge-views','facing':'east'})
    args={'image':'upload:22222222-2222-4222-8222-222222222222','target':f'character:{cid}:east'}
    receipt=prepare(tmp_path,source,args['image'],args['target'],provider_gap='pixellab: no upload download',actor='TEST',reason='x')
    downloaded=tmp_path/'downloaded';shutil.copytree(tmp_path/'views',downloaded)
    with pytest.raises(ValueError,match='pending'):
        verify_character(tmp_path,cid,downloaded)
    confirm(tmp_path,receipt['id'],downloaded/'east.png')
    assert character_sync_reasons(tmp_path,cid)
    assert verify_character(tmp_path,cid,downloaded)['status']=='verified'
    assert not character_sync_reasons(tmp_path,cid)
    animation={'character_id':cid,'mode':'pixminimax','frame_count':16,'directions':['east'],'action_description':'fires laser'}
    assert check_call('mcp__pixellab__animate_character',animation,tmp_path).decision=='allow'


@pytest.mark.parametrize('mode_args',[[],['--uploaded-file','fixed.png','--no-upload-readback','pixellab: no download']])
def test_prepare_cli_requires_exactly_one_readback_mode(tmp_path,mode_args):
    from spritewarden.publication_cli import main
    source,cid=setup_project(tmp_path)
    with pytest.raises(SystemExit) as error:
        main(['--project',str(tmp_path),'prepare','--source',str(source),'--image','upload:22222222-2222-4222-8222-222222222222',
              '--target',f'character:{cid}:east','--by','TEST','--reason','x',*mode_args])
    assert error.value.code==2
    assert not (tmp_path/'.spritewarden/publications').exists()


def test_gap_mode_does_not_bypass_source_facing_or_readiness(tmp_path):
    from spritewarden.publication import prepare
    source,cid=setup_project(tmp_path)
    image='upload:22222222-2222-4222-8222-222222222222'
    with pytest.raises(ValueError,match='facing'):
        prepare(tmp_path,source,image,f'character:{cid}:south',provider_gap='pixellab: no upload download',actor='TEST',reason='x')
    write_record(source,{'direction_set':True,'views':False,'vision_reference':{'passed':False,'status':'fail','metrics':{'status':'mismatch'}}},params={'command':'judge-views','facing':'east'})
    with pytest.raises(ValueError,match='ready'):
        prepare(tmp_path,source,image,f'character:{cid}:east',provider_gap='pixellab: no upload download',actor='TEST',reason='x')


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
