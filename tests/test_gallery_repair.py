import json
from test_gallery_review import console, request


def test_repair_endpoint_is_scoped_and_save_is_new_candidate(console):
    data, base, rev = console
    status, raw = request(base, '/api/repair?asset_id=candidate')
    assert status == 200
    source = json.loads(raw)
    payload = {'asset_id': 'candidate', 'source_digest': source['source_digest'], 'mask': [[0, 0]], 'mapping': {'#ff0000': '#bfbfbf'}, 'actor': 'Tester', 'reason': 'Repair local spot'}
    assert request(base, '/api/repair', payload)[0] == 403
    token = json.loads(request(base, '/api/session')[1])['csrf_token']
    headers = {'Content-Type': 'application/json', 'X-CSRF-Token': token, 'Origin': base}
    status, raw = request(base, '/api/repair', payload, headers)
    assert status == 200
    result = json.loads(raw)
    assert result['changed_pixels'] == 1
    assert result['approval'] == 'pending'
    assert request(base, '/api/repair', payload | {'source_digest': 'stale'}, headers)[0] == 400
    assert request(base, '/repair')[0] == 200
