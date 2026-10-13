"""Opt-in real Chromium test: SPRITEWARDEN_BROWSER_TESTS=1 pytest this file."""
import os
import json
import pytest
from PIL import Image
from test_gallery_review import console


def test_repair_browser_example_and_undo(console, tmp_path):
    if os.environ.get('SPRITEWARDEN_BROWSER_TESTS') != '1':
        pytest.skip('opt-in Chromium repair UI test')
    playwright = pytest.importorskip('playwright.sync_api')
    data, base, rev = console
    example = tmp_path / 'corrected.png'
    im = Image.new('RGBA', (8, 8), 'red'); im.putpixel((0, 0), (191, 191, 191, 255)); im.save(example)
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            errors = []; page.on('pageerror', lambda e: errors.append(str(e)))
            page.goto(base + '/repair?asset_id=candidate')
            playwright.expect(page.locator('#status')).to_contain_text('0 changed pixels')
            assert page.locator('#background').input_value() == '#b6b5a8'
            page.locator('#example').set_input_files(str(example))
            playwright.expect(page.locator('#status')).to_contain_text('1 changed pixels')
            page.locator('#undo').click()
            playwright.expect(page.locator('#status')).to_contain_text('0 changed pixels')
            page.locator('#example').set_input_files(str(example))
            page.locator('#actor').fill('TEST reviewer'); page.locator('#reason').fill('Remove invented pixel')
            page.locator('#save').click()
            playwright.expect(page.locator('#saved')).to_contain_text('Saved 1 changes')
            assert not errors
            files = list((data.project_root / 'bot/.repairs').glob('*/south.png'))
            assert len(files) == 1
            assert Image.open(files[0]).convert('RGBA').tobytes() == im.tobytes()
            assert Image.open(data.project_root / 'bot/south.png').getpixel((0, 0)) == (255, 0, 0, 255)
        finally:
            browser.close()
