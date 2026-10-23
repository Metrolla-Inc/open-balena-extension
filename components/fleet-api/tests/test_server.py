"""Server health and upkeep operations (docker commands mocked), device operations."""
import os
import subprocess
import time

from fleet_api import server
from fleet_api.jobs import InlineJobs
from fleet_api.models import ServerOperation
from fleet_api.server import haproxy_backlog, parse_df_use


def _op(world, body):
    r = world.client.post('/fleet/v1/server/operations', json=body)
    assert r.status_code == 202, r.json
    return world.client.get('/fleet/v1/server/operations/%s' % r.json['id']).json


def _track(container, pidfile, *argv):
    return ['docker', 'exec', container, 'sh', '-c', server._TRACK, pidfile, *argv]


def test_operations_run_the_runbook_docker_commands(world):
    world.start()
    op = _op(world, {'type': 'builder_prune'})
    assert op['state'] == 'succeeded'
    assert world.runner.commands[-1] == _track('open-balena-builder-dind-1', '/tmp/fleet-api-%s.pid' % op['id'],
                                               'docker', 'image', 'prune', '-af')
    assert op['result']['command'] == ['docker', 'exec', 'open-balena-builder-dind-1', 'docker', 'image', 'prune',
                                       '-af']
    _op(world, {'type': 'haproxy_restart'})
    assert world.runner.commands[-1] == ['docker', 'restart', 'open-balena-haproxy-1']
    op = _op(world, {'type': 'registry_prune'})
    assert world.runner.commands[-1][-4:] == ['registry', 'garbage-collect', '--dry-run',
                                              '/etc/distribution/config.yml']
    assert op['result']['command'] == ['docker', 'exec', 'open-balena-registry-1', 'registry', 'garbage-collect',
                                       '--dry-run', '/etc/distribution/config.yml']
    _op(world, {'type': 'registry_prune', 'params': {'dry_run': False}})
    assert '--dry-run' not in world.runner.commands[-1]
    assert world.client.post('/fleet/v1/server/operations', json={'type': 'rm -rf'}).status_code == 422


def test_registry_prune_finds_the_registry_binary_and_config(world):
    world.start()
    # registry:2
    world.runner.registry_args = ['serve', '/etc/docker/registry/config.yml']
    world.runner.registry_files = {'/etc/docker/registry/config.yml'}
    assert _op(world, {'type': 'registry_prune'})['result']['command'][3:] == [
        'registry', 'garbage-collect', '--dry-run', '/etc/docker/registry/config.yml']
    # balena/open-balena-registry: confd-rendered config, started by entry.sh
    world.runner.registry_args = ['/usr/src/app/entry.sh']
    world.runner.registry_bins = ['docker-registry']
    world.runner.registry_files = {'/etc/docker-registry.yml'}
    assert _op(world, {'type': 'registry_prune'})['result']['command'][3:] == [
        'docker-registry', 'garbage-collect', '--dry-run', '/etc/docker-registry.yml']
    # a compose `command:` pointing somewhere else wins over the known paths
    world.runner.registry_args = ['serve', '/config/registry.yaml']
    world.runner.registry_bins = ['registry']
    world.runner.registry_files = {'/config/registry.yaml', '/etc/distribution/config.yml'}
    assert _op(world, {'type': 'registry_prune'})['result']['command'][-1] == '/config/registry.yaml'
    # nothing found: fails without running anything in the container
    world.runner.registry_files = set()
    op = _op(world, {'type': 'registry_prune'})
    assert op['state'] == 'failed' and 'server.registry_config' in op['result']['error']
    assert 'garbage-collect' not in world.runner.commands[-1]


def test_registry_settings_override_detection(world):
    world.start(server=dict(world.doc['server'], registry_bin='registry', registry_config='/srv/reg.yml'))
    world.runner.registry_files = set()
    op = _op(world, {'type': 'registry_prune'})
    assert op['state'] == 'succeeded'
    assert op['result']['command'][-1] == '/srv/reg.yml'
    assert not any(c[1] == 'inspect' or server._PROBE in c for c in world.runner.commands)


def test_a_timed_out_operation_is_stopped_inside_the_container(world):
    world.start()
    world.runner.hang = {'garbage-collect'}
    op = _op(world, {'type': 'registry_prune'})
    pid = '/tmp/fleet-api-%s.pid' % op['id']
    assert op['state'] == 'failed'
    assert op['result']['error'] == 'timed out after 3600s'
    assert op['result']['output'] == 'marking blobs\n'
    assert op['result']['stopped'] == 'stopped 4242'
    assert world.runner.commands[-1] == ['docker', 'exec', 'open-balena-registry-1', 'sh', '-c', server._STOP, pid]
    world.runner.hang = {'prune'}
    op = _op(world, {'type': 'builder_prune'})
    assert op['result']['error'] == 'timed out after 900s'
    assert world.runner.commands[-1][2] == 'open-balena-builder-dind-1'
    assert world.runner.commands[-1][-1] == '/tmp/fleet-api-%s.pid' % op['id']


def test_one_operation_of_a_type_at_a_time(world):
    world.jobs = InlineJobs(defer=True)
    world.start()
    first = world.client.post('/fleet/v1/server/operations', json={'type': 'registry_prune'})
    assert first.status_code == 202
    again = world.client.post('/fleet/v1/server/operations', json={'type': 'registry_prune'})
    assert again.status_code == 409 and again.json['type'].endswith('operation-running')
    assert first.json['id'] in again.json['detail']
    assert world.client.post('/fleet/v1/server/operations', json={'type': 'builder_prune'}).status_code == 202
    world.jobs.run_pending()
    assert world.client.post('/fleet/v1/server/operations', json={'type': 'registry_prune'}).status_code == 202


def test_operations_left_running_by_a_restart_are_stopped(world):
    world.start()
    ops = world.ext['server_ops']
    db = ops.session_factory()
    db.add(ServerOperation(id='sop_old', type='registry_prune', params={}, state='running',
                           result={'command': ['docker', 'exec', 'open-balena-registry-1', 'registry'],
                                   'container': 'open-balena-registry-1', 'pidfile': '/tmp/fleet-api-sop_old.pid'}))
    db.add(ServerOperation(id='sop_queued', type='builder_prune', params={}, state='running'))
    db.commit()
    db.close()
    ops.recover()
    assert world.runner.commands == [['docker', 'exec', 'open-balena-registry-1', 'sh', '-c', server._STOP,
                                      '/tmp/fleet-api-sop_old.pid']]
    old, queued = ops.get('sop_old'), ops.get('sop_queued')
    assert old['state'] == queued['state'] == 'failed'
    assert old['result']['stopped'] == 'stopped 4242' and 'restarted' in old['result']['error']
    assert 'stopped' not in queued['result']
    assert world.client.post('/fleet/v1/server/operations', json={'type': 'registry_prune'}).status_code == 202


def _wait_for(path, present=True, timeout=5):
    deadline = time.time() + timeout
    while os.path.exists(path) != present:
        assert time.time() < deadline, path
        time.sleep(0.02)


def test_tracking_scripts_stop_the_process_and_clean_up(tmp_path):
    """The in-container shell snippets, run with the local sh instead of docker exec."""
    pid = str(tmp_path / 'op.pid')
    p = subprocess.Popen(['sh', '-c', server._TRACK, pid, 'sleep', '30'])
    _wait_for(pid)
    child = int(open(pid).read())
    out = subprocess.run(['sh', '-c', server._STOP, pid], capture_output=True, text=True, timeout=20).stdout
    assert out.strip() == 'stopped %d' % child
    assert p.wait(timeout=5) != 0
    _wait_for(pid, present=False)
    out = subprocess.run(['sh', '-c', server._STOP, pid], capture_output=True, text=True, timeout=5).stdout
    assert out.strip() == 'not running'
    # a command that ends by itself keeps its exit code and removes the pidfile
    assert subprocess.run(['sh', '-c', server._TRACK, pid, 'sh', '-c', 'exit 3']).returncode == 3
    assert not os.path.exists(pid)
    # a pidfile whose process is gone is removed
    open(pid, 'w').write('999999')
    assert subprocess.run(['sh', '-c', server._STOP, pid], capture_output=True, text=True).stdout.strip() == \
        'not running'
    assert not os.path.exists(pid)


def test_probe_script_finds_binary_and_config(tmp_path):
    a, b = tmp_path / 'a.yml', tmp_path / 'b.yml'
    b.write_text('version: 0.1\n')
    out = subprocess.run(['sh', '-c', server._PROBE, 'probe', str(a), str(b)], capture_output=True, text=True,
                         env={'PATH': '/usr/bin:/bin'}).stdout
    assert 'config=%s' % b in out.splitlines()
    assert not any(ln.startswith('bin=') for ln in out.splitlines())


def test_operation_timeout():
    srv = {'command_timeout_s': 900, 'operation_timeouts_s': {'registry_prune': 3600}}
    assert server.operation_timeout('registry_prune', srv) == 3600
    assert server.operation_timeout('builder_prune', srv) == 900
    assert server.operation_timeout('builder_prune', {}) == 900


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


def test_an_unexpected_error_still_finishes_the_operation(world):
    world.start()

    def boom(args, timeout):
        raise RuntimeError('docker socket went away')
    world.runner.run = boom
    op = _op(world, {'type': 'builder_prune'})
    assert op['state'] == 'failed' and op['result']['error'] == 'RuntimeError: docker socket went away'
    assert op['finished_at']
