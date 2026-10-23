"""Sign-in switches and fleet permissions through metrolla_auth."""
from conftest import FLEET_SEG, as_, auth_on

ENV = '/fleet/v1/fleets/%s/env' % FLEET_SEG


def test_auth_off_is_anonymous_site_admin(world):
    world.start()
    me = world.client.get('/fleet/v1/auth/me').json
    assert me['auth_enabled'] is False and me['roles'] == ['site_admin'] and 'fleet:deploy' in me['permissions']
    assert world.client.get('/fleet/v1/fleets').status_code == 200
    assert world.client.patch(ENV, json={'set': {'A': '1'}}).status_code == 200
    assert world.client.post('/fleet/v1/server/operations', json={'type': 'builder_prune'}).status_code == 202


def test_auth_on_requires_a_caller_and_the_route_permission(world):
    world.start(auth=auth_on())
    r = world.client.get('/fleet/v1/fleets')
    assert r.status_code == 401
    assert world.client.get('/fleet/v1/ping').status_code == 200, 'liveness stays public'

    assert world.client.get('/fleet/v1/fleets', headers=as_('viewer')).status_code == 200
    assert world.client.get('/fleet/v1/drift', headers=as_('viewer')).status_code == 200
    r = world.client.patch(ENV, json={'set': {'A': '1'}}, headers=as_('viewer'))
    assert r.status_code == 403 and 'fleet:env' in r.json['detail']
    assert world.client.post('/fleet/v1/changes', json={'items': []}, headers=as_('viewer')).status_code == 403

    assert world.client.patch(ENV, json={'set': {'A': '1'}}, headers=as_('rm')).status_code == 200
    assert world.client.put('/fleet/v1/fleets/%s/target-release' % FLEET_SEG, json={'release': world.r1['id']},
                            headers=as_('rm')).status_code == 200
    assert world.client.post('/fleet/v1/server/operations', json={'type': 'builder_prune'},
                             headers=as_('rm')).status_code == 403
    assert world.client.post('/fleet/v1/server/operations', json={'type': 'builder_prune'},
                             headers=as_('root')).status_code == 202

    wrong = {'Authorization': as_('rm')['Authorization'], 'X-Metrolla-User': 'root'}
    assert world.client.get('/fleet/v1/fleets', headers=wrong).status_code == 401


def test_writes_are_audited_with_the_caller(world):
    world.start(auth=auth_on())
    world.client.patch(ENV, json={'set': {'A': '1'}}, headers=as_('rm'))
    world.client.patch(ENV, json={'set': {'A': '2'}}, headers=as_('viewer'))
    rows = world.client.get('/fleet/v1/audit', headers=as_('root')).json['items']
    by_actor = {(r['actor'], r['outcome'].split()[0]) for r in rows if r['action'].startswith('PATCH')}
    assert ('static:rm', 'ok') in by_actor and ('static:viewer', 'denied') in by_actor
    assert world.client.get('/fleet/v1/audit', headers=as_('rm')).status_code == 403


def test_balena_key_is_never_returned(world):
    world.start()
    for path in ('/fleet/v1/fleets', '/fleet/v1/fleets/%s/releases' % FLEET_SEG, '/fleet/v1/server/health',
                 '/fleet/v1/change-control', '/fleet/v1/swagger.json', '/fleet/v1/drift'):
        assert world.key not in world.client.get(path).get_data(as_text=True), path
