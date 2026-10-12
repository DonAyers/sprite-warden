import pytest
from PIL import Image
from test_views import _set, _view
from spritewarden.judge.views import check_direction_set
from spritewarden.repo.identity import build_identity


def test_explicit_profile_uses_front_even_with_side_anchor():
    identity = build_identity(None, 'thin', prompt_description='thin robot', side_front_area_ratio=[0.60, 0.75])
    frames = _set(east=_view(body_w=11), west=_view(body_w=11))
    assert not check_direction_set(frames).passed
    gate = check_direction_set(frames, identity=identity, anchor=frames['east'], anchor_facing='east')
    assert gate.passed, gate.errors
    damaged = _set(**{f: _view(body_w=5 if f in ('east', 'west') else 8) for f in ('south', 'south_east', 'east', 'north_east', 'north', 'north_west', 'west', 'south_west')})
    assert not check_direction_set(damaged, identity=identity, anchor=_view(body_w=16)).passed
    assert gate.metrics['side_front_area_ratio'] == [0.60, 0.75]


@pytest.mark.parametrize('value', [[], [0.6], [0.6, 0.7, 0.8], [0, 0.7], [-0.1, 0.7], [0.8, 0.6], [0.6, 1.1], [float('nan'), 0.7], [0.6, float('inf')]])
def test_bad_profile_ranges_rejected(value):
    with pytest.raises(ValueError):
        build_identity(None, 'thin', prompt_description='robot', side_front_area_ratio=value)


@pytest.mark.parametrize('value', [-0.1, 1.0, float('nan'), float('inf')])
def test_bad_area_tolerance_rejected(value):
    with pytest.raises(ValueError):
        check_direction_set(_set(), area_tolerance=value)


@pytest.mark.parametrize('facing,view', [('east', _view(body_w=3)), ('west', _view(body_h=15, body_w=11)), ('north', _view(body_w=11)), ('north_east', _view(body_w=11)), ('east', Image.new('RGBA', (48,48)))])
def test_profile_does_not_hide_damage(facing, view):
    identity = build_identity(None, 'thin', prompt_description='robot', side_front_area_ratio=[0.60, 0.75])
    frames = _set(east=_view(body_w=11), west=_view(body_w=11))
    frames[facing] = view
    assert not check_direction_set(frames, identity=identity).passed


@pytest.mark.parametrize('anchor_facing,width', [('south', 16), ('east', 11), ('west', 11), ('north', 16)])
def test_global_body_loss_fails_external_anchor(anchor_facing, width):
    identity = build_identity(None, 'thin', prompt_description='robot', side_front_area_ratio=[0.60, 0.75])
    frames = _set(**{f: _view(body_w=5 if f in ('east', 'west') else 8) for f in ('south', 'south_east', 'east', 'north_east', 'north', 'north_west', 'west', 'south_west')})
    gate = check_direction_set(frames, identity=identity, anchor=_view(body_w=width), anchor_facing=anchor_facing)
    assert not gate.passed
    assert gate.details['checks']['anchor_front_area']['passed'] is False


def test_cli_and_mcp_profile_configuration():
    import inspect
    from spritewarden.server.mcp_server import register_character_identity, judge_direction_set
    import subprocess, sys
    result = subprocess.run([sys.executable, '-m', 'spritewarden.cli', 'identity', 'set', '--help'], capture_output=True, text=True)
    assert '--side-front-area-ratio' in result.stdout
    assert 'side_front_area_ratio' in inspect.signature(register_character_identity).parameters
    assert inspect.signature(judge_direction_set).parameters['area_tolerance'].default == 0.20


def test_profile_pair_is_symmetric_and_requires_front():
    identity = build_identity(None, 'thin', prompt_description='robot', side_front_area_ratio=[0.50, 0.90])
    for e, w in [(9, 11), (11, 9)]:
        gate = check_direction_set(_set(east=_view(body_w=e), west=_view(body_w=w)), identity=identity)
        assert not gate.details['checks']['profile_pair_area']['passed']
    frames = _set(east=_view(body_w=11), west=_view(body_w=11))
    del frames['south']
    assert not check_direction_set(frames, identity=identity).passed


def test_mcp_policy_evidence_and_identity_staleness(tmp_path, monkeypatch):
    from spritewarden.repo.catalog import AssetCatalog
    from spritewarden.server import mcp_server
    from spritewarden.gates.records import load_record, dependency_problems, review_problem
    database = tmp_path / 'catalog.db'
    monkeypatch.setenv('SPRITEWARDEN_DB', str(database))
    catalog = AssetCatalog(database)
    monkeypatch.setattr(mcp_server, 'get_catalog', lambda: catalog)
    result = mcp_server.register_character_identity('thin', name='Thin', prompt_description='robot', palette_id='luap-40', side_front_area_ratio=[0.60, 0.75])
    assert result.get('status') != 'error', result
    folder = tmp_path / 'views'
    folder.mkdir()
    for facing, image in _set(east=_view(body_w=11), west=_view(body_w=11)).items():
        image.save(folder / (facing + '.png'))
    result = mcp_server.judge_direction_set(str(folder), character_id='thin', area_tolerance=0.15,
        anchor_path=str(folder / 'east.png'), anchor_facing='east')
    assert result['passed'], result
    record = load_record(folder / 'east.png')
    assert record['params']['area_tolerance'] == 0.15
    assert record['params']['side_front_area_ratio'] == [0.60, 0.75]
    assert not dependency_problems(record)
    assert review_problem(folder / 'east.png')  # deterministic success is not vision completeness
    mcp_server.register_character_identity('thin', name='Thin', prompt_description='robot', side_front_area_ratio=[0.65, 0.75])
    assert any('identity' in reason for reason in dependency_problems(record))
