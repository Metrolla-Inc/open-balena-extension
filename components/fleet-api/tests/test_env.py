"""Env vars: masking, fleet/service/device/device-service patches, drift, audit."""
from sqlalchemy import select

from conftest import FLEET, FLEET_SEG


def _svc(world, name):
    return next(s for s in world.balena.rows('service') if s['service_name'] == name)


def test_get_masks_secrets(world):
    world.start()
    world.balena.add('application_environment_variable', application=world.fleet['id'], name='SPLUNK_HEC_TOKEN',
                     value='hunter2')
    world.balena.add('application_environment_variable', application=world.fleet['id'], name='METROLLA_AUTH_ENABLED',
                     value='false')
    world.balena.add('service_environment_variable', service=_svc(world, 'edgeapi')['id'], name='DB_PASSWORD',
                     value='pw')
    r = world.client.get('/fleet/v1/fleets/%s/env' % FLEET_SEG)
    assert r.status_code == 200
    rows = {(e['service'], e['name']): e for e in r.json['env']}
    assert rows[('', 'SPLUNK_HEC_TOKEN')]['value'] == '***' and rows[('', 'SPLUNK_HEC_TOKEN')]['secret']
    assert rows[('', 'METROLLA_AUTH_ENABLED')]['value'] == 'false'
    assert rows[('edgeapi', 'DB_PASSWORD')] == {'name': 'DB_PASSWORD', 'value': '***', 'service': 'edgeapi',
                                                'level': 'service', 'secret': True}
    assert 'hunter2' not in r.get_data(as_text=True)


def test_patch_fleet_service_and_device_env_then_drift(world):
    world.start()
    r = world.client.patch('/fleet/v1/fleets/%s/env' % FLEET_SEG,
                           json={'set': {'METROLLA_AUTH_ENABLED': 'true', 'API_TOKEN': 's3cret'}})
    assert r.status_code == 200, r.json
    assert {c['name']: c['to'] for c in r.json['changed']} == {'METROLLA_AUTH_ENABLED': 'true', 'API_TOKEN': '***'}
    assert world.balena.env('application_environment_variable', 'application', world.fleet['id']) == {
        'METROLLA_AUTH_ENABLED': 'true', 'API_TOKEN': 's3cret'}

    r = world.client.patch('/fleet/v1/fleets/%s/env' % FLEET_SEG, json={'service': 'edgeapi', 'set': {'LOG': 'info'}})
    assert r.status_code == 200
    assert world.balena.env('service_environment_variable', 'service', _svc(world, 'edgeapi')['id']) == {'LOG': 'info'}

    d = world.device('amber-field')
    r = world.client.patch('/fleet/v1/devices/%s/env' % d['uuid'], json={'set': {'CANARY': '1'}})
    assert r.status_code == 200
    r = world.client.patch('/fleet/v1/devices/%s/env' % d['uuid'], json={'service': 'edgeapi', 'set': {'X': 'y'}})
    assert r.status_code == 200
    assert world.balena.env('device_environment_variable', 'device', d['id']) == {'CANARY': '1'}
    assert [v['value'] for v in world.balena.rows('device_service_environment_variable')] == ['y']

    assert world.client.get('/fleet/v1/drift').json['drift'] == []
    # Someone uses the balena CLI / dashboard: change one value, delete one secret.
    for row in world.balena.rows('application_environment_variable'):
        if row['name'] == 'METROLLA_AUTH_ENABLED':
            row['value'] = 'false'
    tok = next(r for r in world.balena.rows('application_environment_variable') if r['name'] == 'API_TOKEN')
    del world.balena.db['application_environment_variable'][tok['id']]
    drift = world.client.get('/fleet/v1/drift', query_string={'fleet': FLEET}).json['drift']
    assert {(x['kind'], x['name'], x['expected'], x['actual'], x['detail']) for x in drift} == {
        ('env', 'METROLLA_AUTH_ENABLED', 'true', 'false', 'changed outside the API'),
        ('env', 'API_TOKEN', '***', None, 'removed outside the API')}
    assert 's3cret' not in world.client.get('/fleet/v1/drift').get_data(as_text=True)

    r = world.client.patch('/fleet/v1/fleets/%s/env' % FLEET_SEG, json={'unset': ['API_TOKEN']})
    assert r.status_code == 200
    drift = world.client.get('/fleet/v1/drift').json['drift']
    assert [x['name'] for x in drift] == ['METROLLA_AUTH_ENABLED']


def test_applied_env_keeps_no_secret_values(world):
    from fleet_api.models import AppliedEnv
    world.start()
    world.client.patch('/fleet/v1/fleets/%s/env' % FLEET_SEG, json={'set': {'API_TOKEN': 's3cret', 'MODE': 'a'}})
    db = world.ext['fleet'].session_factory()
    rows = {r.name: r for r in db.execute(select(AppliedEnv)).scalars()}
    db.close()
    assert rows['API_TOKEN'].value is None and rows['API_TOKEN'].value_sha256
    assert rows['MODE'].value == 'a'


def test_env_validation(world):
    world.start()
    url = '/fleet/v1/fleets/%s/env' % FLEET_SEG
    assert world.client.patch(url, json={}).status_code == 422
    assert world.client.patch(url, json={'set': {'1BAD': 'x'}}).status_code == 422
    assert world.client.patch(url, json={'set': {'A': 'x'}, 'unset': ['A']}).status_code == 422
    assert world.client.patch(url, json={'service': 'nope', 'set': {'A': 'x'}}).status_code == 404
    assert world.client.patch('/fleet/v1/fleets/admin%2Fnope/env', json={'set': {'A': 'x'}}).status_code == 404


def test_env_patch_audit_row_is_masked(world):
    world.start()
    world.client.patch('/fleet/v1/fleets/%s/env' % FLEET_SEG, json={'set': {'DB_PASSWD': 'pw1', 'MODE': 'b'}})
    rows = world.client.get('/fleet/v1/audit', query_string={'action': 'PATCH'}).json['items']
    assert rows and rows[0]['after']['set'] == {'DB_PASSWD': '***', 'MODE': 'b'}
    assert rows[0]['actor'] == 'anonymous' and rows[0]['outcome'].startswith('ok')


def test_env_patch_change_set_and_verify_rollback(world):
    world.start(rollout={'update_timeout_min': 30, 'verify_settle_s': 60})
    world.balena.add('application_environment_variable', application=world.fleet['id'], name='MODE', value='old')
    c = world.client.post('/fleet/v1/changes', json={
        'ticket': 'CHG7', 'items': [{'op': 'env.patch', 'fleet': FLEET, 'set': {'MODE': 'new', 'NEW_KEY': 'k'}}]}).json
    assert c['state'] == 'verifying', c
    assert c['plan']['items'][0]['diff'] == [{'name': 'MODE', 'service': None, 'from': 'old', 'to': 'new'},
                                             {'name': 'NEW_KEY', 'service': None, 'from': None, 'to': '***'}]
    world.edge('quiet-river').health = 'failed'          # the env change broke a device
    world.tick(minutes=0.5)
    c = world.change(c['id'])
    assert c['state'] == 'rolled_back' and 'quiet-river health ok -> failed' in c['error']
    assert world.balena.env('application_environment_variable', 'application', world.fleet['id']) == {'MODE': 'old'}
    assert c['items'][0]['set'] == {'MODE': 'new', 'NEW_KEY': '***'}
    assert 'previous' not in str(c['progress'])


def test_env_change_applies_after_the_settle_period(world):
    world.start(rollout={'update_timeout_min': 30, 'verify_settle_s': 60})
    c = world.client.post('/fleet/v1/changes', json={
        'items': [{'op': 'env.patch', 'fleet': FLEET, 'set': {'MODE': 'new'}}]}).json
    world.tick(minutes=0.5)
    assert world.change(c['id'])['state'] == 'verifying'
    world.tick(minutes=1)
    assert world.change(c['id'])['state'] == 'applied'
