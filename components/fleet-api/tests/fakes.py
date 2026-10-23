"""Fakes for the Fleet API tests: an openBalena pine API (with the supervisor proxy and the
remote builder's /v3/build), the VPN service proxy, and device EdgeAPIs behind it. All run as
real HTTP servers on 127.0.0.1 so the production clients are exercised unchanged.
"""
import base64
import gzip
import io
import json
import re
import socketserver
import tarfile
import threading
import uuid as uuidlib
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler

from flask import Flask, Response, request
from werkzeug.serving import make_server

NAV = {
    'application': ('should_be_running__release',),
    'release': ('belongs_to__application',),
    'device': ('belongs_to__application', 'is_running__release', 'is_pinned_on__release'),
    'service': ('application',),
    'image': ('is_a_build_of__service',),
    'release_image': ('release', 'image'),
    'application_environment_variable': ('application',),
    'service_environment_variable': ('service',),
    'device_environment_variable': ('device',),
    'service_install': ('device', 'installs__service'),
    'device_service_environment_variable': ('service_install',),
}


class FakeClock:
    def __init__(self, start=None):
        self.now = start or datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now = self.now + timedelta(**kw)
        return self.now


# ── OData $filter (the subset the client sends) ──
_TOKEN = re.compile(r"\s*(?:(\()|(\))|(,)|('(?:[^']|'')*')|(-?\d+)|([A-Za-z_][A-Za-z0-9_/]*))")


def _tokens(text):
    pos, out = 0, []
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m or m.end() == pos:
            if text[pos:].strip() == '':
                break
            raise ValueError('bad filter near %r' % text[pos:])
        pos = m.end()
        lp, rp, comma, s, num, word = m.groups()
        if lp:
            out.append(('(', None))
        elif rp:
            out.append((')', None))
        elif comma:
            out.append((',', None))
        elif s is not None:
            out.append(('lit', s[1:-1].replace("''", "'")))
        elif num is not None:
            out.append(('lit', int(num)))
        else:
            out.append(('word', word))
    return out


class _Parser:
    def __init__(self, text):
        self.t = _tokens(text)
        self.i = 0

    def peek(self):
        return self.t[self.i] if self.i < len(self.t) else (None, None)

    def take(self):
        tok = self.peek()
        self.i += 1
        return tok

    def parse(self):
        e = self.or_()
        return e

    def or_(self):
        left = self.and_()
        while self.peek() == ('word', 'or'):
            self.take()
            right = self.and_()
            left = ('or', left, right)
        return left

    def and_(self):
        left = self.atom()
        while self.peek() == ('word', 'and'):
            self.take()
            right = self.atom()
            left = ('and', left, right)
        return left

    def value(self):
        kind, v = self.take()
        if kind == 'lit':
            return v
        if kind == 'word' and v in ('true', 'false', 'null'):
            return {'true': True, 'false': False, 'null': None}[v]
        raise ValueError('expected a literal')

    def atom(self):
        kind, v = self.peek()
        if kind == '(':
            self.take()
            e = self.or_()
            assert self.take()[0] == ')'
            return e
        if kind == 'word' and v == 'false':
            self.take()
            return ('const', False)
        if kind == 'word' and v == 'startswith':
            self.take()
            assert self.take()[0] == '('
            field = self.take()[1]
            assert self.take()[0] == ','
            prefix = self.value()
            assert self.take()[0] == ')'
            return ('startswith', field, prefix)
        field = self.take()[1]
        op = self.take()[1]
        assert op == 'eq', op
        return ('eq', field, self.value())


def _eval(e, row):
    kind = e[0]
    if kind == 'const':
        return e[1]
    if kind == 'or':
        return _eval(e[1], row) or _eval(e[2], row)
    if kind == 'and':
        return _eval(e[1], row) and _eval(e[2], row)
    if kind == 'startswith':
        return str(row.get(e[1]) or '').startswith(e[2])
    return row.get(e[1]) == e[2]


class FakeBalena:
    """In-memory openBalena. Navigation fields are stored as plain ids."""

    def __init__(self, api_key, clock):
        self.key = api_key
        self.clock = clock
        self.db = {r: {} for r in NAV}
        self.calls = []
        self.next_id = 100
        self.lock = threading.RLock()
        self.builds = []
        self.build_fails = False
        self.build_fail_status = False
        self.app = self._make_app()
        self.server = make_server('127.0.0.1', 0, self.app, threaded=True)
        self.url = 'http://127.0.0.1:%d' % self.server.server_port
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def close(self):
        self.server.shutdown()

    # ── store ──
    def add(self, resource, **row):
        with self.lock:
            self.next_id += 1
            row.setdefault('id', self.next_id)
            self.db[resource][row['id']] = row
            return row

    def rows(self, resource):
        return list(self.db[resource].values())

    def get_row(self, resource, id_):
        return self.db[resource].get(id_)

    def fleet(self, slug, device_type='jetson-agx-orin-devkit'):
        name = slug.split('/')[-1]
        app = self.add('application', app_name=name, slug=slug, should_be_running__release=None,
                       should_track_latest_release=True, device_type=device_type)
        for svc in ('edgeapi', 'frontend'):
            self.add('service', application=app['id'], service_name=svc)
        return app

    def release(self, app, status='success', final=True, commit=None):
        return self.add('release', belongs_to__application=app['id'], commit=commit or uuidlib.uuid4().hex,
                        status=status, is_final=final, semver='1.0.0', raw_version='1.0.0',
                        created_at=self.clock().isoformat(), end_timestamp=self.clock().isoformat())

    def device(self, app, name, release=None, online=True):
        d = self.add('device', uuid=uuidlib.uuid4().hex, device_name=name, belongs_to__application=app['id'],
                     is_online=online, last_connectivity_event=self.clock().isoformat(),
                     api_heartbeat_state='online' if online else 'offline', status='Idle',
                     is_running__release=release['id'] if release else None, is_pinned_on__release=None,
                     supervisor_version='16.4.0', os_version='balenaOS 5.3.0')
        for svc in self.rows('service'):
            if svc['application'] == app['id']:
                self.add('service_install', device=d['id'], installs__service=svc['id'])
        return d

    def pin_fleet(self, app, release):
        app['should_be_running__release'] = release['id']
        app['should_track_latest_release'] = False

    def simulate_updates(self, stuck=()):
        """Every online device not in `stuck` starts running its target release."""
        with self.lock:
            for d in self.rows('device'):
                if not d['is_online'] or d['uuid'] in stuck or d['device_name'] in stuck:
                    continue
                app = self.db['application'][d['belongs_to__application']]
                target = d['is_pinned_on__release'] or app['should_be_running__release']
                if target:
                    d['is_running__release'] = target

    def env(self, resource, parent_field, parent_id):
        return {r['name']: r['value'] for r in self.rows(resource) if r[parent_field] == parent_id}

    # ── HTTP ──
    def _render(self, resource, row, expand):
        out = {}
        for k, v in row.items():
            if k in NAV[resource]:
                out[k] = {'__id': v} if v is not None else None
            else:
                out[k] = v
        if resource == 'release' and expand and 'release_image' in expand:
            ris = []
            for ri in self.rows('release_image'):
                if ri['release'] == row['id']:
                    img = dict(self.db['image'][ri['image']])
                    svc = self.db['service'][img['is_a_build_of__service']]
                    img['is_a_build_of__service'] = [{'service_name': svc['service_name']}]
                    ris.append({'id': ri['id'], 'image': [img]})
            out['release_image'] = ris
        return out

    def _make_app(self):
        app = Flask('fake-balena')
        fake = self

        def authed():
            return request.headers.get('Authorization') == 'Bearer ' + fake.key

        @app.route('/ping')
        def ping():
            return 'OK'

        @app.route('/v6/<resource>', methods=['GET', 'POST', 'PATCH'])
        def collection(resource):
            if not authed():
                return Response('Unauthorized', status=401)
            fake.calls.append((request.method, request.full_path, request.get_json(silent=True)))
            if resource not in NAV:
                return Response('no such resource', status=404)
            with fake.lock:
                rows = fake.rows(resource)
                flt = request.args.get('$filter')
                if flt:
                    expr = _Parser(flt).parse()
                    rows = [r for r in rows if _eval(expr, r)]
                if request.method == 'POST':
                    row = fake.add(resource, **request.get_json())
                    return fake._render(resource, row, None), 201
                if request.method == 'PATCH':
                    for r in rows:
                        r.update(request.get_json())
                    return Response(status=200)
                order = request.args.get('$orderby')
                if order:
                    field, _, direction = order.partition(' ')
                    rows.sort(key=lambda r: (r.get(field) is None, r.get(field)), reverse=direction == 'desc')
                top = request.args.get('$top')
                if top:
                    rows = rows[:int(top)]
                return {'d': [fake._render(resource, r, request.args.get('$expand')) for r in rows]}

        @app.route('/v6/<resource>(<int:rid>)', methods=['GET', 'PATCH', 'DELETE'])
        def one(resource, rid):
            if not authed():
                return Response('Unauthorized', status=401)
            fake.calls.append((request.method, '/v6/%s(%d)' % (resource, rid), request.get_json(silent=True)))
            with fake.lock:
                row = fake.db[resource].get(rid)
                if row is None:
                    return Response('not found', status=404)
                if request.method == 'PATCH':
                    row.update(request.get_json())
                    return Response(status=200)
                if request.method == 'DELETE':
                    del fake.db[resource][rid]
                    return Response(status=200)
                return {'d': [fake._render(resource, row, None)]}

        @app.route('/supervisor/<path:path>', methods=['POST'])
        def supervisor(path):
            if not authed():
                return Response('Unauthorized', status=401)
            fake.calls.append(('POST', '/supervisor/' + path, request.get_json(silent=True)))
            return 'OK'

        @app.route('/v3/build', methods=['POST'])
        def build():
            if not authed():
                return Response('Unauthorized', status=401)
            slug = request.args['slug']
            raw = gzip.decompress(request.get_data())
            with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
                names = sorted(tar.getnames())
            fake.builds.append({'slug': slug, 'files': names, 'args': dict(request.args)})
            with fake.lock:
                apps = [a for a in fake.rows('application') if a['slug'] == slug]
                if not apps:
                    return Response('no such fleet', status=404)
                app_ = apps[0]
                rel = fake.add('release', belongs_to__application=app_['id'], commit=uuidlib.uuid4().hex,
                               status='running', is_final=False, semver='0.0.0', raw_version='0.0.0',
                               created_at=fake.clock().isoformat(), end_timestamp=None)
                for svc in fake.rows('service'):
                    if svc['application'] == app_['id']:
                        img = fake.add('image', is_a_build_of__service=svc['id'], status='success',
                                       content_hash='sha256:' + uuidlib.uuid4().hex, image_size=1000)
                        fake.add('release_image', release=rel['id'], image=img['id'])

            def stream():
                yield '['
                yield json.dumps({'message': '[Info] Starting build for %s' % slug})
                yield ','
                yield json.dumps({'type': 'metadata', 'resource': 'buildLogId', 'value': str(rel['id'])})
                yield ','
                if fake.build_fails:
                    rel['status'] = 'failed'
                    yield json.dumps({'message': '[Error] Build failed: step 3/7', 'isError': True})
                else:
                    yield json.dumps({'message': '[Info] Uploading images', 'replace': True})
                    yield ','
                    rel['status'] = 'failed' if fake.build_fail_status else 'success'
                    rel['end_timestamp'] = fake.clock().isoformat()
                    yield json.dumps({'message': '[Info] Release: %s (id: %d)' % (rel['commit'], rel['id'])})
                yield ']'
            return Response(stream(), mimetype='application/json')

        return app


class FakeDevice:
    def __init__(self, uuid):
        self.uuid = uuid
        self.health = 'ok'
        self.changes = {}
        self.change_outcome = 'applied'
        self.refuse = False
        self.requests = []


class FakeVpn:
    """The openBalena VPN service proxy plus every device's EdgeAPI behind it."""

    def __init__(self, api_key):
        self.key = api_key
        self.devices = {}
        self.connects = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *a):
                pass

            def do_CONNECT(self):
                host, _, port = self.path.partition(':')
                auth = self.headers.get('Proxy-Authorization', '')
                ok_auth = auth == 'Basic ' + base64.b64encode(('root:%s' % fake.key).encode()).decode()
                uuid = host[:-len('.balena')] if host.endswith('.balena') else None
                fake.connects.append((host, port, ok_auth))
                if not ok_auth:
                    self.send_response_only(407, 'Proxy Authentication Required')
                    self.end_headers()
                    self.close_connection = True
                    return
                if uuid not in fake.devices:
                    self.send_response_only(404, 'Device Not Found')
                    self.end_headers()
                    self.close_connection = True
                    return
                self.wfile.write(b'HTTP/1.0 200 Connection Established\r\n\r\n')
                self.tunnel_uuid = uuid
                self.close_connection = False

            def _device(self):
                return fake.devices.get(getattr(self, 'tunnel_uuid', None))

            def _send(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                d = self._device()
                if d is None:
                    return self._send(400, {'error': 'no tunnel'})
                d.requests.append(('GET', self.path, dict(self.headers)))
                if self.path == '/api/v1/health':
                    return self._send(200, {'site_status': d.health, 'detection_ok': d.health == 'ok'})
                m = re.match(r'^/api/v1/changes/(.+)$', self.path)
                if m and m.group(1) in d.changes:
                    c = d.changes[m.group(1)]
                    c['state'] = d.change_outcome
                    return self._send(200, c)
                return self._send(404, {'title': 'not found'})

            def do_POST(self):
                d = self._device()
                if d is None:
                    return self._send(400, {'error': 'no tunnel'})
                length = int(self.headers.get('Content-Length') or 0)
                body = json.loads(self.rfile.read(length) or b'null')
                d.requests.append(('POST', self.path, dict(self.headers), body))
                if self.path == '/api/v1/changes':
                    if d.refuse:
                        return self._send(409, {'title': 'change control refused'})
                    key = self.headers.get('Idempotency-Key')
                    for c in d.changes.values():
                        if c['idempotency_key'] == key:
                            return self._send(200, c)
                    cid = 'chg_%s' % uuidlib.uuid4().hex[:8]
                    d.changes[cid] = {'id': cid, 'state': 'planned', 'idempotency_key': key, 'body': body}
                    return self._send(201, d.changes[cid])
                return self._send(404, {'title': 'not found'})

        class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
            daemon_threads = True
            allow_reuse_address = True

        self.server = Server(('127.0.0.1', 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def add(self, uuid):
        self.devices[uuid] = FakeDevice(uuid)
        return self.devices[uuid]

    def close(self):
        self.server.shutdown()


class FakeImagemaker:
    def __init__(self, token):
        self.token = token
        self.jobs = {}
        self.requests = []
        app = Flask('fake-imagemaker')
        fake = self

        def authed():
            return request.headers.get('x-imagemaker-token') == fake.token

        @app.route('/api/fleets')
        def fleets():
            if not authed():
                return Response('unauthorized', status=401)
            return json.dumps([{'slug': 'admin/bellagio-retail', 'deviceType': 'jetson-agx-orin-devkit'}])

        @app.route('/api/versions')
        def versions():
            return json.dumps(['6.0.24', '5.3.0'])

        @app.route('/api/build', methods=['POST'])
        def build():
            if not authed():
                return Response('unauthorized', status=401)
            body = request.get_json()
            fake.requests.append(body)
            jid = uuidlib.uuid4().hex[:16]
            fake.jobs[jid] = {'status': 'building', 'message': 'downloading', 'error': None,
                              'name': '%s-%s.img.gz' % (body['fleet'].replace('/', '-'), body['deviceType'])}
            return json.dumps({'id': jid})

        @app.route('/api/status/<jid>')
        def status(jid):
            if jid not in fake.jobs:
                return Response(json.dumps({'error': 'no such job'}), status=404)
            return json.dumps(fake.jobs[jid])

        @app.route('/download/<jid>')
        def download(jid):
            if not authed():
                return Response('unauthorized', status=401)
            return Response(b'IMAGEBYTES', mimetype='application/gzip',
                            headers={'Content-Disposition': 'attachment; filename="x.img.gz"'})

        self.server = make_server('127.0.0.1', 0, app, threaded=True)
        self.url = 'http://127.0.0.1:%d' % self.server.server_port
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def close(self):
        self.server.shutdown()


class HookSink:
    """Collects webhook deliveries."""

    def __init__(self):
        self.events = []
        app = Flask('hooks')
        sink = self

        @app.route('/hook', methods=['POST'])
        def hook():
            sink.events.append({'body': request.get_json(), 'headers': dict(request.headers)})
            return 'ok'

        self.server = make_server('127.0.0.1', 0, app, threaded=True)
        self.url = 'http://127.0.0.1:%d/hook' % self.server.server_port
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def types(self):
        return [e['body']['type'] for e in self.events]

    def close(self):
        self.server.shutdown()


class FakeRunner:
    """Records docker commands instead of running them."""

    def __init__(self, df_use=42):
        self.commands = []
        self.df_use = df_use

    def run(self, args, timeout):
        import subprocess
        self.commands.append(list(args))
        out = 'done'
        if 'df' in args:
            out = ('Filesystem 1024-blocks Used Available Capacity Mounted on\n'
                   'overlay 100 %d %d %d%% /var/lib/docker\n' % (self.df_use, 100 - self.df_use, self.df_use))
        return subprocess.CompletedProcess(args, 0, stdout=out, stderr='')
