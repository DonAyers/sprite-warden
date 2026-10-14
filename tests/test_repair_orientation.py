from PIL import Image
from spritewarden.repair_preflight import inspect_plan


def test_invalid_donor_is_not_eligible_for_jev_under_another_proposal(tmp_path):
    Image.new('RGBA',(16,16),'gray').save(tmp_path/'north.png')
    result=inspect_plan(tmp_path,{'source':'north.png','goal':'rear geometry','defect_kind':'orientation',
        'proposed_action':'geometry_patch','available_actions':['donor_patch','geometry_patch']})
    assert 'donor_patch' not in result['eligible_actions']


def test_relabeling_front_copy_as_geometry_does_not_bypass_block(tmp_path):
    Image.new('RGBA',(16,16),'gray').save(tmp_path/'north.png')
    Image.new('RGBA',(16,16),'gray').save(tmp_path/'south.png')
    result=inspect_plan(tmp_path,{'source':'north.png','goal':'rear geometry','defect_kind':'orientation',
        'target_facing':'north','proposed_action':'geometry_patch','donor':{'image':'south.png','facing':'south',
        'region':[0,0,8,8],'usage':'direct_copy','transforms':['flip_horizontal']}})
    assert result['status']=='blocked'


def test_orientation_report_shows_review_boundary_and_donor(tmp_path):
    from spritewarden.repair_preflight_cli import render_report
    Image.new('RGBA',(16,16),'gray').save(tmp_path/'north.png')
    Image.new('RGBA',(16,16),'gray').save(tmp_path/'south.png')
    result=inspect_plan(tmp_path,{'source':'north.png','goal':'rear reconstruction','defect_kind':'orientation',
        'proposed_action':'geometry_patch','target_facing':'north','donor':{'image':'south.png','facing':'south',
        'region':[0,0,8,8],'usage':'reference','transforms':['redraw']},
        'references':[{'image':'south.png','facing':'south'}]})
    assert result['mechanical_status']=='clear' and result['pose_review_status']=='unverified'
    report=render_report(result)
    assert 'Pose: UNVERIFIED' in report and 'Mechanical: CLEAR' in report
    assert 'Donor — south — reference' in report and 'Reference — south' in report
    assert 'Game scale' in report


def test_save_orientation_repair_requires_plan_binding(tmp_path):
    import pytest
    from spritewarden.repair import save_candidate,make_patch,digest
    before=Image.new('RGBA',(16,16),'gray');after=before.copy();after.putpixel((0,0),(0,0,0,0))
    source=tmp_path/'north.png';before.save(source)
    with pytest.raises(ValueError,match='plan'):
        save_candidate(tmp_path,source,digest(before),[],{},actor='TEST',reason='geometry',patch=make_patch(before,after))
    assert not (tmp_path/'.repairs').exists()


def test_save_geometry_receipt_binds_actual_candidate_and_plan(tmp_path):
    from spritewarden.repair import save_candidate,make_patch,digest
    before=Image.new('RGBA',(16,16),'gray');after=before.copy();after.putpixel((0,0),(0,0,0,0))
    source=tmp_path/'north.png';before.save(source)
    plan={'source':'north.png','goal':'rear elbow','defect_kind':'orientation','proposed_action':'geometry_patch',
          'target_facing':'north','required_regions':{'elbow':[0,0,4,4]}}
    result=save_candidate(tmp_path,source,digest(before),[],{},actor='TEST',reason='geometry',patch=make_patch(before,after),repair_plan=plan)
    assert result['preflight']['mechanical_status']=='clear'
    assert result['preflight']['pose_review_status']=='unverified'
    assert result['preflight']['metrics']['required_region_changes']['elbow']==1
    assert result['repair_plan']['target_facing']=='north'
    assert result['approval']=='pending'
    import hashlib,json
    from pathlib import Path
    stored=json.loads((Path(result['path']).parent/'repair.json').read_text())
    assert stored['preflight']['candidate']==result['path']
    assert stored['preflight']['metrics']['candidate_sha256']==hashlib.sha256(Path(result['path']).read_bytes()).hexdigest()
    assert stored['repair_plan']['target_facing']=='north'


def test_gallery_forwards_plan_and_records_pose_boundary(tmp_path):
    from spritewarden.gallery.data import GalleryData
    from spritewarden.repair import digest,make_patch
    before=Image.new('RGBA',(16,16),'gray');after=before.copy();after.putpixel((0,0),(0,0,0,0))
    source=tmp_path/'north.png';before.save(source)
    registered=[]
    class Catalog:
        def register_asset(self,*args,**kwargs):registered.append(kwargs)
    gallery=GalleryData.__new__(GalleryData);gallery.project_root=tmp_path;gallery.catalog=Catalog()
    gallery.asset=lambda asset_id:{'exists':True,'resolved_path':str(source),'name':'North'}
    plan={'source':'north.png','goal':'rear elbow','defect_kind':'orientation','proposed_action':'geometry_patch',
          'target_facing':'north','required_regions':{'elbow':[0,0,4,4]}}
    result=gallery.repair({'asset_id':'north','source_digest':digest(before),'actor':'TEST','reason':'geometry',
                         'patch':make_patch(before,after),'repair_plan':plan})
    assert result['preflight']['pose_review_status']=='unverified'
    assert registered[0]['provenance']['preflight']['pose_review_status']=='unverified'


def test_gallery_editor_exposes_plan_not_pose_approval():
    from pathlib import Path
    import spritewarden
    folder=Path(spritewarden.__file__).parent/'gallery'/'static'
    assert 'id="repair-plan"' in (folder/'repair.html').read_text()
    assert 'repair_plan:' in (folder/'repair.js').read_text()
    assert 'Pose:' in (folder/'repair.js').read_text()


def test_donor_changed_during_intent_capture_cannot_be_saved(tmp_path,monkeypatch):
    import pytest
    from spritewarden import events
    from spritewarden.repair import digest,make_patch,save_candidate
    before=Image.new('RGBA',(16,16),'gray');after=before.copy();after.putpixel((0,0),(0,0,0,255))
    source=tmp_path/'north.png';before.save(source);before.save(tmp_path/'donor.png')
    plan={'source':'north.png','goal':'rear elbow','defect_kind':'orientation','proposed_action':'donor_patch',
          'target_facing':'north','available_actions':['donor_patch'],
          'donor':{'image':'donor.png','facing':'north','region':[0,0,4,4],'usage':'direct_copy','transforms':['translate']}}
    def emit(*args,**kwargs):
        Image.new('RGBA',(16,16),'red').save(tmp_path/'donor.png');return 'intent'
    monkeypatch.setattr(events,'emit',emit)
    with pytest.raises(ValueError,match='dependency changed'):
        save_candidate(tmp_path,source,digest(before),[],{},actor='TEST',reason='geometry',patch=make_patch(before,after),repair_plan=plan)
    assert not (tmp_path/'.repairs').exists()


def test_mask_changed_during_intent_capture_cannot_be_saved(tmp_path,monkeypatch):
    import pytest
    from spritewarden import events
    from spritewarden.repair import digest,make_patch,save_candidate
    before=Image.new('RGBA',(16,16),'gray');after=before.copy();after.putpixel((0,0),(0,0,0,255))
    source=tmp_path/'north.png';before.save(source)
    mask=tmp_path/'mask.png';Image.new('L',(16,16),255).save(mask)
    plan={'source':'north.png','mask':'mask.png','goal':'rear elbow','defect_kind':'orientation',
          'proposed_action':'geometry_patch','target_facing':'north','required_regions':{'elbow':[0,0,4,4]}}
    def emit(event,*args,**kwargs):
        if event=='repair.started':
            Image.new('L',(16,16),0).save(mask)
        return 'intent'
    monkeypatch.setattr(events,'emit',emit)
    with pytest.raises(ValueError,match='dependency changed'):
        save_candidate(tmp_path,source,digest(before),[],{},actor='TEST',reason='geometry',patch=make_patch(before,after),repair_plan=plan)
    assert not (tmp_path/'.repairs').exists()


def test_valid_same_facing_donor_remains_unverified(tmp_path):
    Image.new('RGBA',(16,16),'gray').save(tmp_path/'north.png')
    Image.new('RGBA',(16,16),'gray').save(tmp_path/'donor.png')
    result=inspect_plan(tmp_path,{'source':'north.png','goal':'rear elbow','defect_kind':'orientation',
        'target_facing':'north','proposed_action':'donor_patch','available_actions':['donor_patch'],
        'donor':{'image':'donor.png','facing':'north','region':[0,0,4,4],'usage':'direct_copy','transforms':['translate']}})
    assert result['status']=='proposal' and result['pose_review_status']=='unverified'
    assert result['donor']['sha256']


def test_invalid_donor_paths_regions_and_labels_fail_closed(tmp_path):
    import pytest
    Image.new('RGBA',(16,16),'gray').save(tmp_path/'north.png')
    donor={'image':'north.png','facing':'north','region':[0,0,4,4],'usage':'direct_copy','transforms':['translate']}
    for change in [{'image':'../outside.png'},{'facing':'back'},{'region':[False,0,4,4]},
                   {'region':[0,0,17,4]},{'usage':'approved'},{'transforms':['rotate_to_back']}]:
        with pytest.raises(ValueError):
            inspect_plan(tmp_path,{'source':'north.png','goal':'rear elbow','defect_kind':'orientation',
                'target_facing':'north','proposed_action':'donor_patch','donor':{**donor,**change}})


def test_blocked_donor_cannot_save_even_when_actual_elbow_pixels_change(tmp_path):
    import pytest
    from spritewarden.repair import digest,make_patch,save_candidate
    before=Image.new('RGBA',(16,16),'gray');after=before.copy();after.putpixel((0,0),(0,0,0,255))
    source=tmp_path/'north.png';before.save(source);before.save(tmp_path/'south.png')
    plan={'source':'north.png','goal':'rear elbow','defect_kind':'orientation','target_facing':'north',
        'proposed_action':'donor_patch','available_actions':['donor_patch'],'required_regions':{'elbow':[0,0,1,1]},
        'donor':{'image':'south.png','facing':'south','region':[0,0,4,4],'usage':'direct_copy','transforms':['flip_horizontal']}}
    with pytest.raises(ValueError,match='front/back'):
        save_candidate(tmp_path,source,digest(before),[],{},actor='TEST',reason='geometry',patch=make_patch(before,after),repair_plan=plan)
    assert not (tmp_path/'.repairs').exists()


def test_front_donor_horizontal_flip_is_not_a_back_view(tmp_path):
    Image.new('RGBA',(16,16),'gray').save(tmp_path/'north.png')
    Image.new('RGBA',(16,16),'gray').save(tmp_path/'south.png')
    result=inspect_plan(tmp_path,{
        'source':'north.png','goal':'Reconstruct rear-view elbow, not mirrored front surfaces',
        'defect_kind':'orientation','proposed_action':'donor_patch','target_facing':'north',
        'available_actions':['donor_patch','geometry_patch'],
        'donor':{'image':'south.png','facing':'south','region':[0,0,8,8],
                 'usage':'direct_copy','transforms':['flip_horizontal','translate']}})
    assert result['status']=='blocked'
    assert 'donor_patch' not in result['eligible_actions']
    assert 'front/back' in result['excluded_actions']['donor_patch']
    assert result['pose_review_status']=='unverified'
    assert result['execution_authorized'] is False
