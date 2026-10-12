import json
import threading
import urllib.request
import urllib.error
import pytest
from PIL import Image
from spritewarden.gallery.data import GalleryData
from spritewarden.gallery.server import make_server
from spritewarden.repo.catalog import AssetCatalog
from spritewarden.gates.records import write_record
from spritewarden import workflow_state as ws


@pytest.fixture
def console(tmp_path):
    folder = tmp_path / 'bot'; folder.mkdir()
    path = folder / 'south.png'; Image.new('RGBA', (8, 8), 'red').save(path)
    write_record(path, {'start_image': {'passed': False}})
    rev = ws.register_revision(tmp_path, folder, stage='anchor', files={'south': 'bot/south.png'},
        provider='test', operation='create', expected_version=0)
    cat = AssetCatalog(tmp_path / 'catalog.db')
    cat.register_asset('candidate', 'candidate', path)
    data = GalleryData(cat, project_root=tmp_path)
    server = make_server(data, port=0, quiet=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    base = 'http://127.0.0.1:' + str(server.server_address[1])
    yield data, base, rev
    server.shutdown(); server.server_close(); thread.join()


def request(base, route, payload=None, headers=None):
    req = urllib.request.Request(base + route, data=json.dumps(payload).encode() if payload is not None else None,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as response:
        return response.code, response.read()


def test_console_failed_candidate_readable_and_secure_decision(console):
    data, base, rev = console
    assert request(base, '/img/candidate')[0] == 200
    status, raw = request(base, '/api/review')
    assert status == 200
    review = json.loads(raw)
    pipeline = review['pipelines'][0]
    assert pipeline['state']['revisions'][rev['revision_id']]['files']['south']['evidence']['gates']['start_image']['passed'] is False
    payload = {'pipeline_id': pipeline['id'], 'revision_id': rev['revision_id'], 'expected_version': 1,
               'decision': 'rejected', 'actor': 'owner', 'reason': 'wrong silhouette'}
    assert request(base, '/api/decisions', payload)[0] == 403
    token = json.loads(request(base, '/api/session')[1])['csrf_token']
    headers = {'Content-Type': 'application/json', 'X-CSRF-Token': token, 'Origin': base}
    assert request(base, '/api/decisions', payload, headers | {'Origin': 'https://evil.example'})[0] == 403
    assert request(base, '/api/decisions', payload, headers | {'Host': 'evil.example'})[0] == 403
    assert request(base, '/api/decisions', payload, headers | {'Content-Type': 'text/plain'})[0] == 415
    assert request(base, '/api/decisions', payload | {'pipeline_id': '../elsewhere'}, headers)[0] == 400
    assert request(base, '/api/decisions', payload, headers)[0] == 200
    assert request(base, '/api/decisions', payload, headers)[0] == 409
    events = json.loads(request(base, '/api/trace')[1])
    assert any(e['event_type'] == 'human.decision' for e in events)
    assert request(base, '/api/session', headers={'Host': 'evil.example'})[0] == 403


def test_review_images_are_exact_revision_and_diff_mask(console):
    import io
    data, base, rev = console
    pipeline = data.review()['pipelines'][0]
    prefix = '/api/revision-image?pipeline_id=' + pipeline['id'] + '&revision_id=' + rev['revision_id'] + '&facing=south'
    original = request(base, prefix)
    assert original[0] == 200
    Image.new('RGBA', (8, 8), 'blue').save(data.project_root / 'bot/south.png')
    assert request(base, prefix)[1] == original[1]
    assert request(base, prefix.replace('facing=south', 'facing=../../secret'))[0] == 400
    query = '/api/compare?before=candidate&after=candidate'
    assert json.loads(request(base, query)[1])['changed_pixels'] == 0
    status, raw = request(base, query + '&mask=1')
    assert status == 200
    assert Image.open(io.BytesIO(raw)).getbbox() is None
    assert request(base, '/review')[0] == 200
    assert b'frame-step' in request(base, '/static/review.js')[1]


def test_mutation_body_limit_and_nonlocal_bind(console):
    data, base, rev = console
    token = json.loads(request(base, '/api/session')[1])['csrf_token']
    headers = {'Origin': base, 'Content-Type': 'application/json', 'X-CSRF-Token': token}
    assert request(base, '/api/decisions', {'reason': 'x' * 33000}, headers)[0] == 413
    with pytest.raises(ValueError, match='loopback'):
        make_server(data, host='0.0.0.0', port=0)
    with urllib.request.urlopen(base + '/api/session') as response:
        assert response.headers['X-Content-Type-Options'] == 'nosniff'
        assert "frame-ancestors 'none'" in response.headers['Content-Security-Policy']
