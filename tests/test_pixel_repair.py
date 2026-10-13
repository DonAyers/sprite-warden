from PIL import Image


def test_save_candidate_never_overwrites_source_or_copies_approval(tmp_path):
    from spritewarden.repair import save_candidate, digest
    p = tmp_path / 'original.png'
    Image.new('RGBA', (3, 3), '#25562e').save(p)
    original = p.read_bytes()
    result = save_candidate(tmp_path, p, digest(Image.open(p)), [(0, 0)], {'#25562e': '#bfbfbf'}, actor='Tester', reason='Remove invented spot')
    assert p.read_bytes() == original
    assert result['changed_pixels'] == 1
    assert result['approval'] == 'pending'
    assert result['validation']['pixel_grid']['passed'] is True
    assert result['validation']['alpha_preserved'] is True
    assert not __import__('pathlib').Path(result['path'] + '.gate.json').exists()
    with pytest.raises(ValueError, match='changed'):
        save_candidate(tmp_path, p, 'wrong', [(0, 0)], {'#25562e': '#bfbfbf'}, actor='Tester', reason='Repair')

def test_example_save_keeps_exact_shading(tmp_path):
    from spritewarden.repair import save_candidate, digest, make_patch
    p = tmp_path / 'original.png'
    a = Image.new('RGBA', (2, 2), '#25562e'); a.save(p)
    b = a.copy(); b.putpixel((0, 0), (191, 191, 191, 255)); b.putpixel((1, 0), (109, 109, 109, 255))
    result = save_candidate(tmp_path, p, digest(a), [], {}, actor='Tester', reason='Example patch', patch=make_patch(a, b))
    assert Image.open(result['path']).convert('RGBA').tobytes() == b.tobytes()


def test_repair_refuses_outside_project_and_failed_capture(tmp_path, monkeypatch):
    from spritewarden.repair import save_candidate, digest
    from spritewarden import events
    folder = tmp_path / 'project'; folder.mkdir()
    p = tmp_path / 'outside.png'; Image.new('RGBA', (2, 2), 'red').save(p)
    with pytest.raises(ValueError, match='outside'):
        save_candidate(folder, p, digest(Image.open(p)), [(0, 0)], {'#ff0000':'#bbbbbb'}, actor='Tester', reason='Repair')
    p = folder / 'inside.png'; Image.new('RGBA', (2, 2), 'red').save(p)
    monkeypatch.setattr(events, 'emit', lambda *a, **k: None)
    with pytest.raises(RuntimeError, match='intent'):
        save_candidate(folder, p, digest(Image.open(p)), [(0, 0)], {'#ff0000':'#bbbbbb'}, actor='Tester', reason='Repair')
    assert not (folder / '.repairs').exists()


def test_invalid_mask_cannot_change_unselected_pixels():
    from spritewarden.repair import recolor
    with pytest.raises(ValueError, match='outside'):
        recolor(Image.new('RGBA',(2,2),'red'),[(-1,0)],{(255,0,0):(0,0,0)})


import pytest


def test_masked_recolor_preserves_other_pixels_and_alpha():
    from spritewarden.repair import recolor
    im = Image.new('RGBA', (3, 2), '#25562e')
    im.putpixel((0, 0), (37, 86, 46, 90))
    out = recolor(im, [(0, 0)], {(37, 86, 46): (191, 191, 191)})
    assert out.getpixel((0, 0)) == (191, 191, 191, 90)
    assert out.getpixel((1, 0)) == im.getpixel((1, 0))
    assert im.getpixel((0, 0)) == (37, 86, 46, 90)


def test_example_patch_is_exact_source_bound():
    from spritewarden.repair import make_patch, apply_patch
    a = Image.new('RGBA', (2, 2), '#25562e')
    b = a.copy(); b.putpixel((0, 0), (191, 191, 191, 255))
    patch = make_patch(a, b)
    assert list(apply_patch(a, patch).getdata()) == list(b.getdata())
    a.putpixel((1, 1), (0, 0, 0, 255))
    with pytest.raises(ValueError, match='source'):
        apply_patch(a, patch)
