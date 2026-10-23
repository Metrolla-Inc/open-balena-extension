"""DeviceReach through the VPN proxy (CONNECT <uuid>.balena:80) and site change fan-out."""
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from conftest import FLEET_SEG
from fleet_api.reach import ConnectivityReach, VpnProxyReach
from metrolla_change_control import public_key_b64, sign_change

SITE_CHANGE = {'title': 'zones v4', 'ticket': 'CHG55', 'base_rev': 118,
               'items': [{'op': 'config.patch', 'layer': 'site', 'section': 'zones', 'patch': {'a': 1}}],
               'window': 'next'}


def _device(world, name):
    d = world.device(name)
    return {'uuid': d['uuid'], 'online': True, 'name': name}


def test_connect_through_the_vpn_proxy(world):
    reach = VpnProxyReach('127.0.0.1', world.vpn.port, world.key, device_token='tok', device_user='fleet-api')
    h = reach.health(_device(world, 'quiet-river'))
    assert h.status == 'ok' and h.source == 'edgeapi'
    host, port, ok = world.vpn.connects[-1]
    assert host == world.device('quiet-river')['uuid'] + '.balena' and port == '80' and ok
    _, path, headers = world.edge('quiet-river').requests[-1]
    assert path == '/api/v1/health' and headers['Authorization'] == 'Bearer tok'
    assert headers['X-Metrolla-User'] == 'fleet-api'


def test_proxy_failures_fall_back_to_connectivity(world):
    wrong_key = VpnProxyReach('127.0.0.1', world.vpn.port, 'not-the-key')
    h = wrong_key.health(_device(world, 'quiet-river'))
    assert h.source == 'connectivity' and h.status == 'ok' and '407' in h.detail
    unknown = VpnProxyReach('127.0.0.1', world.vpn.port, world.key)
    h = unknown.health({'uuid': 'f' * 32, 'online': True})
    assert h.source == 'connectivity' and '404' in h.detail
    down = VpnProxyReach('127.0.0.1', 1, world.key, timeout_s=1)
    assert down.health(_device(world, 'quiet-river')).source == 'connectivity'
    assert ConnectivityReach().health({'online': False, 'last_seen': 'x'}).status == 'failed'
    assert ConnectivityReach().health({'online': True, 'api_heartbeat_state': 'timeout'}).status == 'degraded'


def _fanout(world, **extra):
    body = dict({'title': 'zones to the fleet', 'ticket': 'CHG55', 'change': SITE_CHANGE,
                 'waves': [{'devices': ['wandering-mountain']}, {'percent': 100}], 'soak_min': 10}, **extra)
    return world.client.post('/fleet/v1/fleets/%s/site-changes' % FLEET_SEG, json=body)


def test_site_change_fans_out_in_waves(world):
    world.start()
    world.device('stone-bridge')['is_online'] = False
    r = _fanout(world)
    assert r.status_code == 201, r.json
    c = r.json
    canary = world.edge('wandering-mountain')
    assert [x[1] for x in canary.requests if x[0] == 'POST'] == ['/api/v1/changes']
    assert canary.requests[-1][3] == SITE_CHANGE and canary.requests[-1][2]['Idempotency-Key'].startswith('fsc-')
    assert not [x for x in world.edge('quiet-river').requests if x[0] == 'POST'], 'wave 2 waits'
    world.tick()                              # canary reports applied -> soak
    world.tick(minutes=11)                    # soak over -> wave 2
    world.tick()                              # wave 2 sent
    assert [x for x in world.edge('quiet-river').requests if x[0] == 'POST']
    world.tick()
    world.tick(minutes=11)
    world.tick()
    c = world.change(c['id'])
    assert c['state'] == 'applied', c
    assert c['devices'] == {'updated': 3, 'pending': 0, 'failed': 0, 'skipped': 1}
    assert 'body' not in str(c['progress']['items'][0]['fanout'])


def test_site_change_halts_when_a_site_rolls_back(world):
    world.start()
    world.edge('wandering-mountain').change_outcome = 'rolled_back'
    c = _fanout(world).json
    world.tick()
    c = world.change(c['id'])
    assert c['state'] == 'failed' and 'ended rolled_back' in c['error']
    assert not [x for x in world.edge('quiet-river').requests if x[0] == 'POST']


def test_site_change_refused_by_a_device_halts(world):
    world.start()
    world.edge('wandering-mountain').refuse = True
    c = _fanout(world).json
    assert c['state'] == 'failed' and 'refused the change (HTTP 409)' in c['error']


def test_site_change_outer_signature(world):
    key = Ed25519PrivateKey.generate()
    world.start(change_control={'require_signature': True,
                                'trusted_keys': [{'kid': 'ci', 'ed25519': public_key_b64(key)}]})
    body = {'ticket': 'CHG55', 'change': SITE_CHANGE, 'waves': [{'percent': 100}]}
    assert world.client.post('/fleet/v1/fleets/%s/site-changes' % FLEET_SEG, json=body).status_code == 403
    signed = dict(body, signature=sign_change(body, 'ci', key))
    r = world.client.post('/fleet/v1/fleets/%s/site-changes' % FLEET_SEG, json=signed)
    assert r.status_code == 201 and r.json['signature_kid'] == 'ci'
