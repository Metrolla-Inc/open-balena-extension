"""openBalena server health and upkeep operations.

Health: API /ping, the registry's /v2/ (200 or 401 = up), the VPN proxy port, haproxy's
monitor URI (/health is 503 when api, registry or vpn has no server) and, when a stats URL is
set, its queued requests per backend, the builder answering HTTP, and builder-dind disk use.

Operations run docker on the host through the mounted socket, exactly as the runbook does:
  builder_prune    docker exec <builder-dind> docker image prune -af
  haproxy_restart  docker restart <haproxy>
  registry_prune   docker exec <registry> <registry binary> garbage-collect [--dry-run] <config>
registry_prune defaults to --dry-run; pass {"dry_run": false} to delete. The registry binary and
config are found inside the container (stock registry:3, registry:2 and openBalena's own image
differ) unless server.registry_bin / server.registry_config are set.

docker exec has no kill and killing the docker client leaves the process running in the
container, so an in-container command writes its pid to /tmp/fleet-api-<op id>.pid; on timeout,
or when the API finds the operation still running after a restart, that pid is stopped.
"""
import csv
import io
import json
import logging
import re
import socket
import subprocess
import threading
from typing import Callable, List, Optional, Tuple

import requests

from .models import ServerOperation, iso, new_id, utcnow
from .settings import read_secret

log = logging.getLogger('fleet_api')

OPERATIONS = ('registry_prune', 'builder_prune', 'haproxy_restart')
MAX_OUTPUT = 16 * 1024
REGISTRY_BINARIES = ('registry', 'docker-registry')
# registry:3 (distribution v3), registry:2, balena/open-balena-registry (confd-rendered).
REGISTRY_CONFIGS = ('/etc/distribution/config.yml', '/etc/docker/registry/config.yml', '/etc/docker-registry.yml')

# sh -c <script> <pidfile> <argv...>: run argv as a child so its pid can be recorded and the
# pidfile removed when it ends, however it ends.
_TRACK = '"$@" & echo $! > "$0"; wait $!; rc=$?; rm -f "$0"; exit $rc'
_STOP = ('[ -f "$0" ] || { echo "not running"; exit 0; }; p=$(cat "$0"); kill "$p" 2>/dev/null || '
         '{ rm -f "$0"; echo "not running"; exit 0; }; i=0; while kill -0 "$p" 2>/dev/null; do '
         'if [ "$i" -ge 10 ]; then kill -9 "$p" 2>/dev/null; echo "killed $p"; exit 0; fi; '
         'sleep 1; i=$((i+1)); done; echo "stopped $p"')
_PROBE = ('for b in ' + ' '.join(REGISTRY_BINARIES) + '; do if command -v "$b" >/dev/null 2>&1; then '
          'echo "bin=$b"; break; fi; done; for p in "$@"; do if [ -f "$p" ]; then echo "config=$p"; break; fi; done')


class OperationError(Exception):
    pass


class CommandRunner:
    """subprocess.run with a timeout; tests replace it."""

    def run(self, args: List[str], timeout: int) -> subprocess.CompletedProcess:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def _text(value) -> str:
    if isinstance(value, bytes):
        return value.decode('utf-8', 'replace')
    return value or ''


def find_registry(runner: CommandRunner, server: dict) -> Tuple[str, str]:
    """The registry binary and config path in the registry container; settings win."""
    binary, config = server.get('registry_bin') or '', server.get('registry_config') or ''
    if binary and config:
        return binary, config
    docker, name = server.get('docker', 'docker'), server['containers']['registry']
    candidates = list(REGISTRY_CONFIGS)
    try:
        p = runner.run([docker, 'inspect', '--format', '{{json .Args}}', name], timeout=30)
        args = json.loads(p.stdout or 'null') if p.returncode == 0 else None
        if isinstance(args, list):
            candidates = [a for a in args if isinstance(a, str) and a.endswith(('.yml', '.yaml'))] + candidates
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    try:
        p = runner.run([docker, 'exec', name, 'sh', '-c', _PROBE, 'probe', *candidates], timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise OperationError('could not look inside %s: %s' % (name, e))
    if p.returncode != 0:
        raise OperationError('could not look inside %s: %s' % (name, (_text(p.stderr).strip() or
                                                                      'exit %d' % p.returncode)[:500]))
    found = dict(ln.split('=', 1) for ln in _text(p.stdout).splitlines() if '=' in ln)
    binary, config = binary or found.get('bin', ''), config or found.get('config', '')
    if not binary:
        raise OperationError('no registry binary (%s) in %s; set server.registry_bin' % (
            ', '.join(REGISTRY_BINARIES), name))
    if not config:
        raise OperationError('none of %s exists in %s; set server.registry_config' % (', '.join(candidates), name))
    return binary, config


def operation_command(op: str, params: dict, server: dict,
                      registry: Optional[Tuple[str, str]] = None) -> Tuple[Optional[str], List[str]]:
    """(container, argv) for a command run inside a container; (None, argv) for a host docker command."""
    docker, names = server.get('docker', 'docker'), server['containers']
    if op == 'builder_prune':
        return names['builder_dind'], ['docker', 'image', 'prune', '-af']
    if op == 'haproxy_restart':
        return None, [docker, 'restart', names['haproxy']]
    if op == 'registry_prune':
        binary, config = registry
        cmd = [binary, 'garbage-collect']
        if params.get('dry_run', True):
            cmd.append('--dry-run')
        return names['registry'], cmd + [config]
    raise ValueError('unknown operation %r' % op)


def pidfile(op_id: str) -> str:
    return '/tmp/fleet-api-%s.pid' % op_id


def exec_tracked(docker: str, container: str, pid_path: str, argv: List[str]) -> List[str]:
    return [docker, 'exec', container, 'sh', '-c', _TRACK, pid_path, *argv]


def exec_stop(docker: str, container: str, pid_path: str) -> List[str]:
    return [docker, 'exec', container, 'sh', '-c', _STOP, pid_path]


def operation_timeout(op: str, server: dict) -> int:
    per_op = server.get('operation_timeouts_s') or {}
    return int(per_op.get(op) or server.get('command_timeout_s', 900))


def op_to_dict(o: ServerOperation) -> dict:
    return {'id': o.id, 'type': o.type, 'params': o.params, 'state': o.state, 'result': o.result,
            'created_by': o.created_by, 'created_at': iso(o.created_at), 'finished_at': iso(o.finished_at)}


class ServerOps:
    def __init__(self, session_factory, settings: Callable, runner: CommandRunner, jobs):
        self.session_factory = session_factory
        self.settings = settings
        self.runner = runner
        self.jobs = jobs
        self._lock = threading.Lock()

    def running(self, op: str) -> Optional[str]:
        db = self.session_factory()
        try:
            row = db.query(ServerOperation).filter_by(type=op, state='running').first()
            return row.id if row else None
        finally:
            db.close()

    def start(self, op: str, params: dict, actor: str) -> dict:
        """The new operation, or None when one of this type is already running."""
        with self._lock:
            if self.running(op):
                return None
            db = self.session_factory()
            try:
                row = ServerOperation(id=new_id('sop'), type=op, params=params, state='running', created_by=actor)
                db.add(row)
                db.commit()
                out = op_to_dict(row)
            finally:
                db.close()
        self.jobs.submit(self._run, out['id'])
        return out

    def get(self, op_id: str) -> Optional[dict]:
        db = self.session_factory()
        try:
            row = db.get(ServerOperation, op_id)
            return op_to_dict(row) if row else None
        finally:
            db.close()

    def _stop(self, docker: str, container: str, pid_path: str) -> str:
        try:
            p = self.runner.run(exec_stop(docker, container, pid_path), timeout=60)
            return (_text(p.stdout) + _text(p.stderr)).strip() or 'exit %d' % p.returncode
        except (OSError, subprocess.TimeoutExpired) as e:
            return 'could not stop it: %s' % e

    def recover(self):
        """Operations still 'running' in the database belong to a previous process (one process
        runs them): stop whatever they left in their container and mark them failed."""
        db = self.session_factory()
        try:
            docker = self.settings()['server'].get('docker', 'docker')
            for row in db.query(ServerOperation).filter_by(state='running').all():
                result = dict(row.result or {})
                if result.get('container') and result.get('pidfile'):
                    result['stopped'] = self._stop(docker, result['container'], result['pidfile'])
                result['error'] = 'interrupted: the Fleet API restarted while it ran'
                row.state, row.result, row.finished_at = 'failed', result, utcnow()
                log.warning('server operation %s (%s) was interrupted by a restart', row.id, row.type)
            db.commit()
        finally:
            db.close()

    def _run(self, op_id: str):
        db = self.session_factory()
        try:
            row = db.get(ServerOperation, op_id)
            try:
                self._execute(db, row)
            except Exception as e:
                # Never leave it 'running': that would refuse every later operation of this type.
                log.exception('server operation %s failed', op_id)
                db.rollback()
                row = db.get(ServerOperation, op_id)
                row.state, row.finished_at = 'failed', utcnow()
                row.result = dict(row.result or {}, error='%s: %s' % (type(e).__name__, e))
                db.commit()
        finally:
            db.close()

    def _execute(self, db, row: ServerOperation):
        op_id = row.id
        server = self.settings()['server']
        docker = server.get('docker', 'docker')
        try:
            registry = find_registry(self.runner, server) if row.type == 'registry_prune' else None
        except OperationError as e:
            row.state, row.result, row.finished_at = 'failed', {'error': str(e)}, utcnow()
            db.commit()
            return
        container, argv = operation_command(row.type, row.params or {}, server, registry)
        shown = [docker, 'exec', container, *argv] if container else argv
        result = {'command': shown}
        if container:
            result.update(container=container, pidfile=pidfile(op_id))
            cmd = exec_tracked(docker, container, result['pidfile'], argv)
        else:
            cmd = argv
        row.result = dict(result)
        db.commit()
        timeout = operation_timeout(row.type, server)
        try:
            p = self.runner.run(cmd, timeout=timeout)
            out = (_text(p.stdout) + _text(p.stderr))[-MAX_OUTPUT:]
            row.state = 'succeeded' if p.returncode == 0 else 'failed'
            result.update(exit_code=p.returncode, output=out)
        except subprocess.TimeoutExpired as e:
            row.state = 'failed'
            result.update(error='timed out after %ds' % timeout,
                          output=(_text(e.stdout) + _text(e.stderr))[-MAX_OUTPUT:])
            if container:
                result['stopped'] = self._stop(docker, container, result['pidfile'])
        except OSError as e:
            row.state = 'failed'
            result['error'] = str(e)
        row.result = result
        row.finished_at = utcnow()
        db.commit()


def _status(ok: bool, detail, warn: bool = False) -> dict:
    return {'status': 'ok' if ok and not warn else ('degraded' if ok else 'failed'), 'detail': detail}


def _http(session, url: str, verify, auth=None, timeout=5):
    return session.get(url, timeout=timeout, verify=verify, auth=auth)


def parse_df_use(text: str) -> Optional[int]:
    lines = [ln for ln in (text or '').strip().splitlines() if ln.strip()]
    if len(lines) < 2:
        return None
    m = re.search(r'(\d+)%', lines[-1])
    return int(m.group(1)) if m else None


def haproxy_backlog(csv_text: str) -> dict:
    out = {}
    reader = csv.reader(io.StringIO(csv_text.lstrip('# ')))
    header = next(reader, None)
    if not header or 'qcur' not in header:
        return out
    pi, si, qi = header.index('pxname'), header.index('svname'), header.index('qcur')
    for row in reader:
        if len(row) > qi and row[si] == 'BACKEND':
            out[row[pi]] = int(row[qi] or 0)
    return out


def server_health(balena, settings: dict, runner: CommandRunner, session=None) -> dict:
    session = session or requests.Session()
    server, reach = settings['server'], settings['reach']
    verify = settings['balena'].get('verify', True)
    comps = {}
    comps['api'] = _status(balena.ping(), balena.api_url + '/ping')

    try:
        r = _http(session, server['registry_url'], verify)
        comps['registry'] = _status(r.status_code in (200, 401), 'HTTP %d' % r.status_code)
    except requests.RequestException as e:
        comps['registry'] = _status(False, type(e).__name__)

    try:
        with socket.create_connection((reach['proxy_host'], int(reach['proxy_port'])), timeout=5):
            comps['vpn'] = _status(True, 'proxy %s:%s accepts connections' % (reach['proxy_host'], reach['proxy_port']))
    except OSError as e:
        comps['vpn'] = _status(False, 'proxy %s:%s: %s' % (reach['proxy_host'], reach['proxy_port'], e))

    try:
        r = _http(session, server['haproxy_health_url'], verify)
        detail = {'health': r.status_code}
        ok = r.status_code == 200
        warn = False
        if server.get('haproxy_stats_url'):
            cred = read_secret(server.get('haproxy_stats_auth_file') or '')
            auth = tuple(cred.split(':', 1)) if ':' in cred else None
            try:
                s = _http(session, server['haproxy_stats_url'], False, auth=auth)
                backlog = haproxy_backlog(s.text) if s.status_code == 200 else {}
                detail['backlog'] = backlog
                warn = sum(backlog.values()) > 0
            except requests.RequestException as e:
                detail['backlog'] = 'stats unavailable: %s' % type(e).__name__
        comps['haproxy'] = _status(ok, detail, warn)
    except requests.RequestException as e:
        comps['haproxy'] = _status(False, type(e).__name__)

    try:
        r = _http(session, server['builder_url'], verify)
        builder_up, builder_detail = True, 'HTTP %d' % r.status_code
    except requests.RequestException as e:
        builder_up, builder_detail = False, type(e).__name__
    disk = None
    try:
        p = runner.run([server.get('docker', 'docker'), 'exec', server['containers']['builder_dind'], 'df', '-P',
                        '/var/lib/docker'], timeout=30)
        disk = parse_df_use(p.stdout) if p.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        disk = None
    detail = {'http': builder_detail, 'disk_used_pct': disk}
    if disk is not None and disk >= int(server.get('builder_disk_fail_pct', 90)):
        comps['builder'] = _status(False, dict(detail, hint='run builder_prune'))
    else:
        warn = disk is None or disk >= int(server.get('builder_disk_warn_pct', 80))
        comps['builder'] = _status(builder_up, detail, warn)

    worst = 'ok'
    for c in comps.values():
        if c['status'] == 'failed':
            worst = 'failed'
        elif c['status'] == 'degraded' and worst == 'ok':
            worst = 'degraded'
    return {'status': worst, 'components': comps}
