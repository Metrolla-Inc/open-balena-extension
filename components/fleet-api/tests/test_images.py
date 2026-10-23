"""Flashable images through the (fake) imagemaker."""
from conftest import FLEET


def test_image_build_status_and_download(world):
    world.start()
    r = world.client.post('/fleet/v1/images', json={'fleet': FLEET, 'config': {'network': 'wifi', 'wifi_ssid': 'yard',
                                                                              'wifi_key': 'psk-secret'}})
    assert r.status_code == 202, r.json
    img = r.json
    sent = world.imagemaker.requests[-1]
    assert sent == {'fleet': FLEET, 'deviceType': 'jetson-agx-orin-devkit', 'version': '6.0.24', 'network': 'wifi',
                    'connectivity': 'internet', 'ssid': 'yard', 'key': 'psk-secret'}
    assert 'psk-secret' not in str(img) and img['status'] == 'building' and img['download_url'] is None
    assert world.client.get('/fleet/v1/images/%s/download' % img['id']).status_code == 404

    world.imagemaker.jobs[list(world.imagemaker.jobs)[-1]]['status'] = 'done'
    img = world.client.get('/fleet/v1/images/%s' % img['id']).json
    assert img['status'] == 'ready' and img['download_url'].endswith('/download')
    r = world.client.get(img['download_url'])
    assert r.status_code == 200 and r.data == b'IMAGEBYTES'

    audit = world.client.get('/fleet/v1/audit', query_string={'action': 'POST /fleet/v1/images'}).json['items']
    assert audit and 'psk-secret' not in str(audit)


def test_image_failure_and_lost_job(world):
    world.start()
    img = world.client.post('/fleet/v1/images', json={'fleet': FLEET, 'device_type': 'raspberrypi4-64',
                                                      'os_version': '5.3.0'}).json
    world.imagemaker.jobs.clear()
    img = world.client.get('/fleet/v1/images/%s' % img['id']).json
    assert img['status'] == 'failed' and 'restarted' in img['error']
    assert world.client.post('/fleet/v1/images', json={}).status_code == 422
