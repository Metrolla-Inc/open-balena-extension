"""Release builds through the (fake) remote builder, finalize, and pinning."""
from conftest import FLEET, FLEET_SEG


def _build(world, ref='v2.2.0', path='bellagio-retail', repo='balena-fleets'):
    r = world.client.post('/fleet/v1/fleets/%s/releases' % FLEET_SEG,
                          json={'git': {'repo': repo, 'ref': ref, 'path': path}})
    return r


def test_build_streams_to_the_builder_then_finalizes(world):
    world.start()
    r = _build(world)
    assert r.status_code == 202, r.json
    build = world.client.get('/fleet/v1/builds/%s' % r.json['id']).json
    assert build['status'] == 'success', build
    assert build['finalized'] is True and build['release_id'] and len(build['source_commit']) == 40

    sent = world.balena.builds[-1]
    assert sent['slug'] == FLEET and sent['args']['headless'] == 'false' and sent['args']['isdraft'] == 'false'
    assert 'docker-compose.yml' in sent['files'] and 'edgeapi/Dockerfile' in sent['files']
    assert 'notes.txt' not in sent['files'], '.dockerignore is honoured'
    assert not any(f.startswith('.git/') for f in sent['files'])

    rel = world.balena.get_row('release', build['release_id'])
    assert rel['is_final'] is True and rel['status'] == 'success'
    finalize = [c for c in world.balena.calls if c[0] == 'PATCH' and c[1] == '/v6/release(%d)' % rel['id']]
    assert finalize and finalize[0][2] == {'is_final': True}

    log = world.client.get('/fleet/v1/builds/%s/log' % build['id']).get_data(as_text=True)
    assert 'Starting build' in log and 'finalized' in log and world.key not in log

    releases = world.client.get('/fleet/v1/fleets/%s/releases' % FLEET_SEG).json['releases']
    row = next(x for x in releases if x.get('kind') == 'release' and x['id'] == rel['id'])
    assert row['finalized'] and row['build_id'] == build['id'] and {i['service'] for i in row['images']} == {
        'edgeapi', 'frontend'}
    assert any(x.get('kind') == 'build' and x['id'] == build['id'] for x in releases)


def test_failed_build_is_not_finalized(world):
    world.start()
    world.balena.build_fails = True
    build = world.client.get('/fleet/v1/builds/%s' % _build(world).json['id']).json
    assert build['status'] == 'failed' and not build['finalized'] and 'error' in build['error']
    assert not [c for c in world.balena.calls if c[0] == 'PATCH' and c[1].startswith('/v6/release(')]


def test_git_source_rules(world):
    world.start()
    assert _build(world, repo='other-repo').status_code == 403
    assert _build(world, repo='https://evil.example/x.git').status_code == 422
    assert _build(world, path='../etc').status_code == 422
    bad_ref = world.client.get('/fleet/v1/builds/%s' % _build(world, ref='no-such-tag').json['id']).json
    assert bad_ref['status'] == 'failed' and 'git clone failed' in bad_ref['error']


def test_pin_fleet_and_device(world):
    world.start()
    r2 = world.balena.release(world.fleet)
    r = world.client.put('/fleet/v1/fleets/%s/target-release' % FLEET_SEG, json={'release': r2['commit'][:8]})
    assert r.status_code == 200, r.json
    assert r.json['previous_release_id'] == world.r1['id'] and world.fleet['should_be_running__release'] == r2['id']
    assert world.fleet['should_track_latest_release'] is False

    d = world.device('quiet-river')
    r = world.client.put('/fleet/v1/devices/%s/target-release' % d['uuid'][:10], json={'release': world.r1['id']})
    assert r.status_code == 200 and d['should_be_running__release'] == world.r1['id']
    r = world.client.put('/fleet/v1/devices/%s/target-release' % d['uuid'], json={'release': None})
    assert r.status_code == 200 and d['should_be_running__release'] is None

    draft = world.balena.release(world.fleet, final=False)
    assert world.client.put('/fleet/v1/fleets/%s/target-release' % FLEET_SEG,
                            json={'release': draft['id']}).status_code == 409
    assert world.client.put('/fleet/v1/fleets/%s/target-release' % FLEET_SEG,
                            json={'release': 999999}).status_code == 404


def test_fleets_and_devices_views(world):
    world.start()
    world.devices[3]['is_online'] = False
    world.edge('quiet-river').health = 'degraded'
    fleets = world.client.get('/fleet/v1/fleets').json['fleets']
    assert fleets == [{'id': world.fleet['id'], 'slug': FLEET, 'name': 'bellagio-retail',
                       'target_release_id': world.r1['id'], 'target_release': world.r1['commit'],
                       'tracks_latest': False, 'device_count': 4, 'online_count': 3}]
    devices = {d['name']: d for d in world.client.get('/fleet/v1/fleets/%s/devices' % FLEET_SEG).json['devices']}
    assert devices['wandering-mountain']['release'] == world.r1['commit']
    assert devices['wandering-mountain']['health'] == {'status': 'ok', 'source': 'edgeapi',
                                                       'detail': {'site_status': 'ok', 'detection_ok': True}}
    assert devices['quiet-river']['health']['status'] == 'degraded'
    assert devices['stone-bridge']['health']['status'] == 'failed'
    assert devices['stone-bridge']['health']['source'] == 'connectivity'
    assert world.key not in world.client.get('/fleet/v1/fleets/%s/devices' % FLEET_SEG).get_data(as_text=True)
