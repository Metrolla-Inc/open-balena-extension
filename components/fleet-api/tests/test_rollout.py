"""Fleet change sets that build and roll out in waves with soak, halt and re-pin."""
from conftest import FLEET

DEPLOY = {'op': 'release.deploy', 'fleet': FLEET, 'release': '@built',
          'waves': [{'devices': ['wandering-mountain']}, {'percent': 50}, {'percent': 100}],
          'soak_min': 30, 'halt_if': {'health': 'worse', 'offline_devices': 1}}
BUILD = {'op': 'release.build', 'fleet': FLEET,
         'git': {'repo': 'balena-fleets', 'ref': 'v2.2.0', 'path': 'bellagio-retail'}}


def _post(world, items, **extra):
    body = dict({'title': 'datablade v2.2.0 to bellagio-retail', 'ticket': 'CHG0012400', 'items': items,
                 'window': 'next'}, **extra)
    return world.client.post('/fleet/v1/changes', json=body, headers={'X-Metrolla-User': 'release-pipeline'})


def _pins(world):
    return {d['device_name']: d['is_pinned_on__release'] for d in world.devices}


def test_build_then_wave_rollout_with_soak(world):
    world.start()
    r = _post(world, [BUILD, DEPLOY])
    assert r.status_code == 201, r.json
    c = r.json
    assert c['state'] == 'applying' and c['build']['state'] == 'success'
    new = c['build']['release_id']
    assert world.balena.get_row('release', new)['is_final'] is True

    world.tick()                             # pin the canary
    assert _pins(world)['wandering-mountain'] == new
    assert sum(1 for v in _pins(world).values() if v) == 1
    world.tick()                             # canary runs the release -> soaking
    c = world.change(c['id'])
    assert c['wave'] == 1 and c['progress']['items'][1]['rollout']['waves'][0]['state'] == 'soaking'
    world.tick(minutes=29)
    assert world.change(c['id'])['progress']['items'][1]['rollout']['current'] == 0, 'still soaking'
    world.tick(minutes=2)                    # soak over -> wave 2 (50% = 2 devices incl. the canary)
    world.tick()                             # baseline + prior pins
    world.tick()                             # pin
    world.tick()                             # updated -> soaking
    assert sum(1 for v in _pins(world).values() if v == new) == 2
    c = world.change(c['id'])
    assert c['wave'] == 2 and c['devices']['updated'] == 2
    world.tick(minutes=31)
    world.tick()
    world.tick()
    world.tick()
    world.tick(minutes=31)                   # last wave soaked -> fleet moves, pins dropped -> verifying
    world.tick()                             # verify
    c = world.change(c['id'])
    assert c['state'] == 'applied', c
    assert world.fleet['should_be_running__release'] == new
    assert all(v is None for v in _pins(world).values())
    assert all(d['is_running__release'] == new for d in world.devices)
    assert c['verify_result']['passed'] is True
    for state in ('planned', 'approved', 'applying', 'verifying', 'applied'):
        assert 'fleet.change.%s' % state in world.hooks.types()
    assert all(e['body']['ticket'] == 'CHG0012400' for e in world.hooks.events)
    assert world.client.get('/fleet/v1/drift').json['drift'] == []


def test_worse_canary_health_halts_and_repins_previous(world):
    world.start()
    world.edge('quiet-river').health = 'degraded'
    c = _post(world, [BUILD, DEPLOY]).json
    new = c['build']['release_id']
    world.tick()
    world.tick()
    world.edge('wandering-mountain').health = 'failed'     # the new release breaks the canary
    world.tick(minutes=5)
    c = world.change(c['id'])
    assert c['state'] == 'rolled_back', c
    assert 'health got worse on wandering-mountain: ok -> failed' in c['error']
    assert all(v is None for v in _pins(world).values()), 'canary back on its previous (fleet) release'
    assert world.fleet['should_be_running__release'] == world.r1['id']
    world.tick()
    world.balena.simulate_updates()
    assert all(d['is_running__release'] == world.r1['id'] for d in world.devices)
    assert 'fleet.change.rolled_back' in world.hooks.types()
    assert world.balena.get_row('release', new)['is_final'], 'the built release stays'


def test_offline_devices_halt(world):
    world.start()
    c = _post(world, [BUILD, dict(DEPLOY, waves=[{'percent': 50}, {'percent': 100}])]).json
    world.tick()
    world.tick()
    world.device('amber-field')['is_online'] = False
    world.tick(minutes=1)
    c = world.change(c['id'])
    assert c['state'] == 'rolled_back' and 'went offline: amber-field' in c['error']


def test_update_timeout_halts(world):
    world.start()
    c = _post(world, [BUILD, DEPLOY]).json
    world.tick()
    world.tick(stuck=('wandering-mountain',))
    world.tick(minutes=31, stuck=('wandering-mountain',))
    c = world.change(c['id'])
    assert c['state'] == 'rolled_back' and 'not on the release after 30 min: wandering-mountain' in c['error']


def test_deploy_existing_release_and_one_rollout_per_fleet(world):
    world.start()
    r2 = world.balena.release(world.fleet)
    first = _post(world, [{'op': 'release.deploy', 'fleet': FLEET, 'release': r2['id'],
                           'waves': [{'percent': 100}], 'soak_min': 10}]).json
    assert first['state'] == 'applying'
    second = _post(world, [{'op': 'env.patch', 'fleet': FLEET, 'set': {'LOG_LEVEL': 'debug'}}]).json
    assert second['state'] == 'approved', 'queued behind the running rollout'
    r = world.client.put('/fleet/v1/fleets/admin%2Fbellagio-retail/target-release', json={'release': r2['id']})
    assert r.status_code == 409 and r.json['type'].endswith('rollout-in-progress')
    world.tick()
    world.tick()
    world.tick(minutes=11)
    world.tick()
    assert world.change(first['id'])['state'] == 'applied'
    world.tick()
    world.tick()
    assert world.change(second['id'])['state'] == 'applied'
    assert world.balena.env('application_environment_variable', 'application', world.fleet['id']) == {
        'LOG_LEVEL': 'debug'}


def test_canary_only_rollout_leaves_fleet_target(world):
    world.start()
    r2 = world.balena.release(world.fleet)
    c = _post(world, [{'op': 'release.deploy', 'fleet': FLEET, 'release': r2['commit'],
                       'waves': [{'devices': ['stone-bridge']}], 'soak_min': 0}]).json
    for _ in range(4):
        world.tick()
    assert world.change(c['id'])['state'] == 'applied'
    assert world.fleet['should_be_running__release'] == world.r1['id']
    assert _pins(world)['stone-bridge'] == r2['id']
    assert world.client.get('/fleet/v1/drift').json['drift'] == []


def test_unknown_wave_device_is_a_validation_error(world):
    world.start()
    r = _post(world, [dict(DEPLOY, release=world.r1['id'], waves=[{'devices': ['no-such-device']}])])
    assert r.status_code == 422 and r.json['errors'][0]['pointer'] == '/items/0/waves/0/devices/0'
    r = _post(world, [dict(DEPLOY)])
    assert r.status_code == 422 and r.json['errors'][0]['pointer'] == '/items/0/release'


def test_manual_rollback_of_an_applied_change(world):
    world.start()
    r2 = world.balena.release(world.fleet)
    c = _post(world, [{'op': 'env.patch', 'fleet': FLEET, 'set': {'FEATURE_X': 'on'}},
                      {'op': 'release.deploy', 'fleet': FLEET, 'release': r2['id'], 'waves': [{'percent': 100}]}]).json
    for _ in range(4):
        world.tick()
    assert world.change(c['id'])['state'] == 'applied'
    r = world.client.post('/fleet/v1/changes/%s/rollback' % c['id'], json={'reason': 'customer request'})
    assert r.status_code == 200 and r.json['state'] == 'rolled_back'
    assert world.fleet['should_be_running__release'] == world.r1['id']
    assert world.balena.env('application_environment_variable', 'application', world.fleet['id']) == {}
