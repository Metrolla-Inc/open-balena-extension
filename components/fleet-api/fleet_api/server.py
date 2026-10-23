"""openBalena server health and upkeep operations.

Health: API /ping, the registry's /v2/ (200 or 401 = up), the VPN proxy port, haproxy's
monitor URI (/health is 503 when api, registry or vpn has no server) and, when a stats URL is
set, its queued requests per backend, the builder answering HTTP, and builder-dind disk use.

Operations run docker on the host through the mounted socket, exactly as the runbook does:
  builder_prune    docker exec <builder-dind> docker image prune -af
  haproxy_restart  docker restart <haproxy>
  registry_prune   docker exec <registry> registry garbage-collect [--dry-run] <config>
registry_prune defaults to --dry-run; pass {"dry_run": false} to delete. Its config path and
container name are settings (server.registry_config, server.containers.registry) to verify on
.183 before the first real run.
"""
import csv
import io
import logging
import re
import socket
import subprocess
from typing import Callable, List, Optional

import requests

from .models import ServerOperation, iso, new_id, utcnow
from .settings import read_secret

log = logging.getLogger('fleet_api')

OPERATIONS = ('registry_prune', 'builder_prune', 'haproxy_restart')
MAX_OUTPUT = 16 * 1024


class CommandRunner:
    """subprocess.run with a timeout; tests replace it."""

    def run(self, args: List[str], timeout: int) -> subprocess.CompletedProcess:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def operation_command(op: str, params: dict, server: dict) -> List[str]:
    docker, names = server.get('docker', 'docker'), server['containers']
    if op == 'builder_prune':
        return [docker, 'exec', names['builder_dind'], 'docker', 'image', 'prune', '-af']
    if op == 'haproxy_restart':
        return [docker, 'restart', names['haproxy']]
    if op == 'registry_prune':
        cmd = [docker, 'exec', names['registry'], 'registry', 'garbage-collect']
        if params.get('dry_run', True):
            cmd.append('--dry-run')
        return cmd + [server['registry_config']]
    raise ValueError('unknown operation %r' % op)


def op_to_dict(o: ServerOperation) -> dict:
    return {'id': o.id, 'type': o.type, 'params': o.params, 'state': o.state, 'result': o.result,
            'created_by': o.created_by, 'created_at': iso(o.created_at), 'finished_at': iso(o.finished_at)}


class ServerOps:
    def __init__(self, session_factory, settings: Callable, runner: CommandRunner, jobs):
        self.session_factory = session_factory
        self.settings = settings
        self.runner = runner
        self.jobs = jobs

    def start(self, op: str, params: dict, actor: str) -> dict:
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

    def _run(self, op_id: str):
        db = self.session_factory()
        try:
            row = db.get(ServerOperation, op_id)
            server = self.settings()['server']
            cmd = operation_command(row.type, row.params or {}, server)
            try:
                p = self.runner.run(cmd, timeout=int(server.get('command_timeout_s', 900)))
                out = ((p.stdout or '') + (p.stderr or ''))[-MAX_OUTPUT:]
                row.state = 'succeeded' if p.returncode == 0 else 'failed'
                row.result = {'command': cmd, 'exit_code': p.returncode, 'output': out}
            except subprocess.TimeoutExpired:
                row.state, row.result = 'failed', {'command': cmd, 'error': 'timed out'}
            except OSError as e:
                row.state, row.result = 'failed', {'command': cmd, 'error': str(e)}
            row.finished_at = utcnow()
            db.commit()
        finally:
            db.close()


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
