"""Server health and upkeep operations (docker commands mocked), device operations."""
from fleet_api.server import haproxy_backlog, parse_df_use


def _op(world, body):
    r = world.client.post('/fleet/v1/server/operations', json=body)
    assert r.status_code == 202, r.json
    return world.client.get('/fleet/v1/server/operations/%s' % r.json['id']).json


def test_operations_run_the_runbook_docker_commands(world):
    world.start()
    op = _op(world, {'type': 'builder_prune'})
    assert op['state'] == 'succeeded'
    assert world.runner.commands[-1] == ['docker', 'exec', 'open-balena-builder-dind-1', 'docker', 'image', 'prune',
                                         '-af']
    _op(world, {'type': 'haproxy_restart'})
    assert world.runner.commands[-1] == ['docker', 'restart', 'open-balena-haproxy-1']
    _op(world, {'type': 'registry_prune'})
    assert world.runner.commands[-1] == ['docker', 'exec', 'open-balena-registry-1', 'registry', 'garbage-collect',
                                         '--dry-run', '/etc/docker/registry/config.yml']
    _op(world, {'type': 'registry_prune', 'params': {'dry_run': False}})
    assert '--dry-run' not in world.runner.commands[-1]
    assert world.client.post('/fleet/v1/server/operations', json={'type': 'rm -rf'}).status_code == 422


def test_server_operations_are_gated_by_policy(world):
    world.start(change_control={'mode': 'gated', 'policy': {'op.server.haproxy_restart': {'approvals': 1}}})
    r = world.client.post('/fleet/v1/server/operations', json={'type': 'haproxy_restart'})
    assert r.status_code == 409
    assert world.client.post('/fleet/v1/server/operations', json={'type': 'builder_prune'}).status_code == 202


def test_server_health(world):
    world.start()
    h = world.client.get('/fleet/v1/server/health').json
    assert h['status'] == 'ok', h
    assert set(h['components']) == {'api', 'registry', 'vpn', 'haproxy', 'builder'}
    assert h['components']['builder']['detail']['disk_used_pct'] == 42
    world.runner.df_use = 93
    h = world.client.get('/fleet/v1/server/health').json
    assert h['status'] == 'failed' and h['components']['builder']['detail']['hint'] == 'run builder_prune'
    world.runner.df_use = 85
    assert world.client.get('/fleet/v1/server/health').json['components']['builder']['status'] == 'degraded'


def test_parsers():
    assert parse_df_use('Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/x 10 5 5 57% /x\n') == 57
    assert parse_df_use('') is None
    csv = '# pxname,svname,qcur,qmax\napi-backend,api,0,0\napi-backend,BACKEND,3,9\nfleet-backend,BACKEND,0,0\n'
    assert haproxy_backlog(csv) == {'api-backend': 3, 'fleet-backend': 0}


def test_device_operations_go_through_the_supervisor_api(world):
    world.start()
    d = world.devices[1]
    r = world.client.post('/fleet/v1/devices/%s/operations' % d['uuid'], json={'type': 'restart_service',
                                                                               'service': 'edgeapi'})
    assert r.status_code == 202
    call = world.balena.calls[-1]
    assert call[1] == '/supervisor/v2/applications/%d/restart-service' % world.fleet['id']
    assert call[2] == {'deviceId': d['id'], 'appId': world.fleet['id'],
                       'data': {'appId': world.fleet['id'], 'serviceName': 'edgeapi'}}
    r = world.client.post('/fleet/v1/devices/%s/operations' % d['uuid'], json={'type': 'reboot', 'force': True})
    assert r.status_code == 202
    assert world.balena.calls[-1][1:] == ('/supervisor/v1/reboot', {'deviceId': d['id'], 'data': {'force': True}})
    assert world.client.post('/fleet/v1/devices/%s/operations' % d['uuid'],
                             json={'type': 'restart_service'}).status_code == 422
    assert world.client.post('/fleet/v1/devices/%s/operations' % d['uuid'],
                             json={'type': 'shutdown'}).status_code == 422
