"""Fleet Control API (F11): /fleet/v1 on the openBalena host.

Sign-in, roles, tokens and audit come from metrolla_auth with the same switches as EdgeAPI.
With no auth config (or auth.enabled: false) every caller is anonymous with anonymous_role
site_admin, exactly as EdgeAPI behaves today.

Run one process (gunicorn -w 1 --threads 8): the change scheduler lives in it.
"""
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from flask import Flask, Response, request, stream_with_context
from flask_restx import Api, Resource
from sqlalchemy.orm import sessionmaker

from metrolla_auth import AuthService, StaticConfigProvider, YamlFileConfigProvider
from metrolla_auth import change_control as cc
from metrolla_auth.errors import AuthError
from metrolla_auth.flask import FlaskAuth, audit_values, current_principal, public, require
from metrolla_auth.flask_routes import browser_blueprint, register_restx

from .balena import BalenaClient, BalenaError
from .builder import CliBuilder, HttpBuilder
from .builds import BuildRunner, build_to_dict
from .changes import ChangeError, FleetChanges
from .env import EnvError, masked, validate_patch
from .fanout import FanoutEngine
from .gitsrc import GitError, GitSource, validate as validate_git
from .images import ImageError, ImagemakerClient, ImageService
from .jobs import ThreadJobs
from .models import init_db, make_engine
from .reach import make_reach
from .rollout import RolloutEngine, WaveError
from .server import OPERATIONS, CommandRunner, ServerOps, server_health
from .service import FleetService, NotFound
from .settings import SettingsProvider, read_secret
from .webhooks import Notifier

log = logging.getLogger('fleet_api')

PREFIX = '/fleet/v1'
SIGNIN_PAGE = '/fleet/signin'


def utcnow():
    return datetime.now(timezone.utc)


def _problem(status, code, title, detail=None, errors=None):
    return AuthError(status, code, title, detail, errors)


def _verify(settings) -> object:
    v = settings['balena'].get('verify', True)
    return v if isinstance(v, (bool, str)) else True


class Components:
    """Everything a request or the scheduler needs, built from the current settings. Clients
    are rebuilt when the settings file changes."""

    def __init__(self, settings: SettingsProvider, session_factory, jobs, clock, runner,
                 balena_factory: Optional[Callable] = None, reach_factory: Optional[Callable] = None,
                 builder_factory: Optional[Callable] = None, imagemaker_factory: Optional[Callable] = None,
                 git_factory: Optional[Callable] = None, data_dir: str = '/data', http_session=None):
        self.settings_provider = settings
        self.session_factory = session_factory
        self.jobs = jobs
        self.clock = clock
        self.runner = runner
        self.data_dir = data_dir
        self.http_session = http_session
        self._cache = {}
        self._balena_factory = balena_factory
        self._reach_factory = reach_factory
        self._builder_factory = builder_factory
        self._imagemaker_factory = imagemaker_factory
        self._git_factory = git_factory

    def settings(self):
        return self.settings_provider.current()

    def _cached(self, name, build):
        s = self.settings()
        hit = self._cache.get(name)
        if hit is None or hit[0] is not s:
            hit = (s, build(s))
            self._cache[name] = hit
        return hit[1]

    def api_key(self, s=None):
        s = s or self.settings()
        return read_secret(s['balena'].get('api_key_file') or '', s['balena'].get('api_key_env') or '')

    def balena(self) -> BalenaClient:
        if self._balena_factory:
            return self._balena_factory()
        return self._cached('balena', lambda s: BalenaClient(s['balena']['api_url'], self.api_key(s), _verify(s),
                                                             s['balena'].get('timeout_s', 30), self.http_session))

    def reach(self):
        if self._reach_factory:
            return self._reach_factory()
        return self._cached('reach', lambda s: make_reach(s['reach'], self.api_key(s),
                                                          read_secret(s['reach'].get('device_token_file') or ''),
                                                          tls_verify=_verify(s)))

    def builder(self):
        if self._builder_factory:
            return self._builder_factory()
        s = self.settings()
        b = s['builder']
        if b.get('mode') == 'cli':
            tld = s['balena']['api_url'].split('://', 1)[-1].split('/', 1)[0]
            tld = tld[4:] if tld.startswith('api.') else tld
            return CliBuilder(b.get('cli', 'balena'), os.path.join(self.data_dir, 'balenarc'), self.api_key(s), tld,
                              int(b.get('timeout_s', 3600)))
        return HttpBuilder(b['url'], self.api_key(s), _verify(s), int(b.get('timeout_s', 3600)), self.http_session)

    def git(self) -> GitSource:
        if self._git_factory:
            return self._git_factory()
        g = self.settings()['git']
        return GitSource(g['repo_base'], g.get('allowed_repos') or (), bool(g.get('allow_urls')),
                         read_secret(g.get('token_file') or ''), g.get('ssh_key_file') or '',
                         int(g.get('timeout_s', 300)))

    def imagemaker(self) -> ImagemakerClient:
        if self._imagemaker_factory:
            return self._imagemaker_factory()
        im = self.settings()['imagemaker']
        return ImagemakerClient(im['url'], read_secret(im.get('token_file') or ''), int(im.get('timeout_s', 60)),
                                self.http_session)


def create_app(settings: Optional[SettingsProvider] = None, auth_provider=None, engine=None, clock=None,
               jobs=None, runner=None, balena_factory=None, reach_factory=None, builder_factory=None,
               imagemaker_factory=None, git_factory=None, start_scheduler: Optional[bool] = None,
               data_dir: Optional[str] = None, http_session=None, sleep=time.sleep) -> Flask:
    data_dir = data_dir or os.environ.get('FLEET_API_DATA_DIR', '/data')
    settings = settings or SettingsProvider(os.environ.get('FLEET_API_CONFIG', '/etc/fleet-api/fleet-api.yaml'))
    engine = engine or make_engine(os.environ.get('FLEET_API_DB', 'sqlite:///%s/fleet-api.db' % data_dir))
    session_factory = init_db(engine)
    clock = clock or utcnow
    jobs = jobs or ThreadJobs()
    runner = runner or CommandRunner()
    auth_provider = auth_provider or YamlFileConfigProvider(os.environ.get('METROLLA_AUTH_CONFIG',
                                                                           '/etc/fleet-api/auth.yaml'))
    comp = Components(settings, session_factory, jobs, clock, runner, balena_factory, reach_factory,
                      builder_factory, imagemaker_factory, git_factory, data_dir, http_session)

    auth_service = AuthService(auth_provider, sessionmaker(bind=engine), data_dir=os.path.join(data_dir, 'auth'))
    fleet = FleetService(session_factory, comp.balena, comp.reach, comp.settings)
    builds = BuildRunner(session_factory, comp.balena, comp.builder, comp.git, jobs,
                         release_wait_s=int(comp.settings()['builder'].get('release_wait_s', 300)), sleep=sleep)
    rollout = RolloutEngine(comp.balena, comp.reach,
                            lambda: int(comp.settings()['rollout'].get('update_timeout_min', 30)),
                            on_target=lambda f, d, rid: fleet.record_target(f, d, rid, 'rollout'))
    fanout = FanoutEngine(comp.balena, comp.reach,
                          lambda: int(comp.settings()['rollout'].get('site_change_timeout_min', 1440)))
    notifier = Notifier(lambda: [*(comp.settings().get('webhooks') or []), *comp.settings().change_control.webhooks],
                        jobs, clock, session=http_session)
    changes = FleetChanges(session_factory, fleet, builds, rollout, fanout, comp.settings, clock, notifier,
                           auth_service.audit, comp.git)
    images = ImageService(session_factory, comp.imagemaker)
    server_ops = ServerOps(session_factory, comp.settings, runner, jobs)

    app = Flask(__name__)
    app.config['RESTX_MASK_SWAGGER'] = False
    app.config['ERROR_404_HELP'] = False
    FlaskAuth(auth_service, features=lambda: {'service': 'fleet-api'}).init_app(app)
    api = Api(app, prefix=PREFIX, doc=PREFIX + '/docs', title='Metrolla Fleet Control API', version='1.0',
              description='Fleets, releases, rollouts, env vars, devices, images and server upkeep for openBalena')
    register_restx(api)
    app.register_blueprint(browser_blueprint(url_prefix='/fleet/auth', signin_page=SIGNIN_PAGE))
    app.extensions['fleet_api'] = {'components': comp, 'changes': changes, 'fleet': fleet, 'builds': builds,
                                   'images': images, 'server_ops': server_ops, 'notifier': notifier}

    @api.errorhandler(ChangeError)
    def _change_error(e):
        return _problem(e.status, e.code, e.title, e.detail, e.errors).problem(), e.status

    @api.errorhandler(NotFound)
    def _not_found(e):
        return _problem(404, 'not-found', 'Not found', 'No %s' % e.what).problem(), 404

    @api.errorhandler(EnvError)
    def _env_error(e):
        title = 'Request is not valid' if e.status == 422 else 'Not possible'
        return _problem(e.status, 'validation' if e.status == 422 else 'conflict', title,
                        errors=[{'pointer': e.pointer, 'message': e.message}]).problem(), e.status

    @api.errorhandler(GitError)
    def _git_error(e):
        return _problem(e.status, 'git', 'Git source refused', e.message,
                        [{'pointer': e.pointer, 'message': e.message}]).problem(), e.status

    @api.errorhandler(WaveError)
    def _wave_error(e):
        return _problem(422, 'validation', 'Request is not valid',
                        errors=[{'pointer': e.pointer, 'message': e.message}]).problem(), 422

    @api.errorhandler(ImageError)
    def _image_error(e):
        return _problem(e.status, 'image', 'Image build refused', e.message).problem(), e.status

    @api.errorhandler(BalenaError)
    def _balena_error(e):
        status = 404 if e.status == 404 else 502
        return _problem(status, 'openbalena', 'openBalena API error', str(e)).problem(), status

    def actor() -> str:
        p = current_principal()
        return p.sub if p else 'unknown'

    def body() -> dict:
        b = request.get_json(silent=True)
        if not isinstance(b, dict):
            raise _problem(422, 'validation', 'Request is not valid', errors=[{'pointer': '', 'message':
                                                                              'must be a JSON object'}])
        return b

    def gate(resource_class: str, fleet_slug: Optional[str] = None):
        cfg = comp.settings().change_control
        if not cc.direct_write_allowed(cfg, resource_class):
            raise _problem(409, 'change-control-required', 'Change control is on',
                           'Direct %s writes are gated here: send them as a fleet change set (POST %s/changes).'
                           % (resource_class, PREFIX))
        if fleet_slug:
            busy = changes.fleet_busy(fleet_slug)
            if busy:
                raise _problem(409, 'rollout-in-progress', 'A change is rolling out on this fleet',
                               'Wait for %s or cancel it.' % busy)

    # ── fleets ──
    @api.route('/fleets')
    class Fleets(Resource):
        @require('fleet:view')
        def get(self):
            """Fleets with target release, device counts and online state."""
            return {'fleets': fleet.list_fleets()}

    @api.route('/fleets/<path:fleet_ref>/releases')
    class Releases(Resource):
        @require('fleet:view')
        def get(self, fleet_ref):
            """Releases with status, commit, images and finalized flag (plus this API's recent builds)."""
            f = fleet.fleet(fleet_ref)
            return {'fleet': f['slug'], 'releases': fleet.list_releases(f)}

        @require('fleet:release')
        def post(self, fleet_ref):
            """Build a release from a git ref through the remote builder, then finalize it (async)."""
            f = fleet.fleet(fleet_ref)
            gate('release.build')
            git = validate_git(body().get('git'))
            comp.git().resolve(git['repo'])
            return builds.start(f, git, actor()), 202

    @api.route('/builds/<string:build_id>')
    class BuildStatus(Resource):
        @require('fleet:view')
        def get(self, build_id):
            """A release build: state, release id, commit, finalized."""
            b = builds.get(build_id)
            if b is None:
                raise NotFound('build %s' % build_id)
            return build_to_dict(b)

    @api.route('/builds/<string:build_id>/log')
    class BuildLog(Resource):
        @require('fleet:view')
        def get(self, build_id):
            """The build output as text."""
            b = builds.get(build_id)
            if b is None:
                raise NotFound('build %s' % build_id)
            return Response(b.log or '', mimetype='text/plain')

    @api.route('/fleets/<path:fleet_ref>/target-release')
    class FleetTarget(Resource):
        @require('fleet:deploy')
        def put(self, fleet_ref):
            """Pin the fleet to a release (gated)."""
            f = fleet.fleet(fleet_ref)
            gate('release.deploy', f['slug'])
            release = fleet.resolve_release(f, body().get('release'))
            return fleet.pin_fleet(f, release, actor())

    @api.route('/fleets/<path:fleet_ref>/devices')
    class Devices(Resource):
        @require('fleet:view')
        def get(self, fleet_ref):
            """Devices with release, supervisor state, online, last seen and health summary
            (?health=false skips the per-device health check)."""
            f = fleet.fleet(fleet_ref)
            with_health = request.args.get('health', 'true').lower() not in ('0', 'false', 'no')
            return {'fleet': f['slug'], 'devices': fleet.list_devices(f, with_health)}

    @api.route('/devices/<string:uuid>/target-release')
    class DeviceTarget(Resource):
        @require('fleet:deploy')
        def put(self, uuid):
            """Pin one device, for canaries ({"release": null} unpins it) (gated)."""
            d = fleet.device(uuid)
            f = fleet.fleet_of(d)
            gate('release.deploy', f['slug'])
            ref = body().get('release')
            release = fleet.resolve_release(f, ref) if ref is not None else None
            return fleet.pin_device(d, release, actor())

    def _masked_patch(b):
        p = fleet.patterns
        out = dict(b)
        if isinstance(b.get('set'), dict):
            out['set'] = {k: masked(k, str(v), p) for k, v in b['set'].items()}
        return out

    @api.route('/fleets/<path:fleet_ref>/env')
    class FleetEnv(Resource):
        @require('fleet:view')
        def get(self, fleet_ref):
            """Fleet and service env vars, secrets masked."""
            f = fleet.fleet(fleet_ref)
            return {'fleet': f['slug'], 'env': fleet.fleet_env(f)}

        @require('fleet:env')
        def patch(self, fleet_ref):
            """Set or remove fleet or service env vars {set, unset, service} (gated)."""
            b = body()
            audit_values(after=_masked_patch(b))
            f = fleet.fleet(fleet_ref)
            gate('env', f['slug'])
            set_vars, unset, service = validate_patch(b)
            res = fleet.patch_env(f, None, service, set_vars, unset, actor())
            res.pop('_previous', None)
            return res

    @api.route('/devices/<string:uuid>/env')
    class DeviceEnv(Resource):
        @require('fleet:env')
        def patch(self, uuid):
            """Device env overrides {set, unset, service} (gated)."""
            b = body()
            audit_values(after=_masked_patch(b))
            d = fleet.device(uuid)
            f = fleet.fleet_of(d)
            gate('env', f['slug'])
            set_vars, unset, service = validate_patch(b)
            res = fleet.patch_env(f, d, service, set_vars, unset, actor())
            res.pop('_previous', None)
            return res

    @api.route('/devices/<string:uuid>/operations')
    class DeviceOps(Resource):
        @require('fleet:device')
        def post(self, uuid):
            """restart_service {service} or reboot {force} through the supervisor API (gated per policy)."""
            b = body()
            op = b.get('type')
            if op not in ('restart_service', 'reboot'):
                raise EnvError('/type', 'type must be restart_service or reboot')
            d = fleet.device(uuid)
            gate('op.device.%s' % op)
            return fleet.device_operation(d, op, b), 202

    # ── images ──
    @api.route('/images')
    class Images(Resource):
        @require('fleet:release')
        def post(self):
            """Build a flashable, fleet-preconfigured image through imagemaker
            {fleet, device_type, os_version, config: {network, connectivity, wifi_ssid, wifi_key}}."""
            b = body()
            cfg = b.get('config') if isinstance(b.get('config'), dict) else {}
            audit_values(after=dict(b, config=dict(cfg, wifi_key='***' if cfg.get('wifi_key') else None)))
            if not b.get('fleet'):
                raise EnvError('/fleet', 'fleet is required')
            f = fleet.fleet(b['fleet'])
            return images.create(f, b, actor()), 202

    @api.route('/images/<string:image_id>')
    class ImageStatus(Resource):
        @require('fleet:view')
        def get(self, image_id):
            """Image build status and download link."""
            img = images.get(image_id)
            if img is None:
                raise NotFound('image %s' % image_id)
            return img

    @api.route('/images/<string:image_id>/download')
    class ImageDownload(Resource):
        @require('fleet:release')
        def get(self, image_id):
            """The .img.gz (it embeds fleet provisioning credentials, hence fleet:release)."""
            job = images.imagemaker_id(image_id)
            if job is None:
                raise NotFound('ready image %s' % image_id)
            r = comp.imagemaker().download(job)
            if r.status_code != 200:
                raise ImageError(502, 'imagemaker answered %d' % r.status_code)
            headers = {k: v for k, v in r.headers.items() if k.lower() in ('content-length', 'content-disposition')}
            return Response(stream_with_context(r.iter_content(1024 * 1024)), mimetype='application/gzip',
                            headers=headers)

    # ── change sets ──
    def principal():
        return current_principal()

    @api.route('/changes')
    class Changes(Resource):
        @require('change:view')
        def get(self):
            """Fleet change sets, newest first (?state=)."""
            try:
                limit = max(1, min(int(request.args.get('limit', 50)), 500))
            except ValueError:
                raise EnvError('/limit', 'must be an integer')
            return {'changes': changes.list(request.args.get('state'), limit)}

        @require('change:author')
        def post(self):
            """Fleet change set: build, env changes, rollout waves."""
            b = body()
            change, created = changes.create(b, principal(), request.headers.get('Idempotency-Key'))
            return change, 201 if created else 200

    @api.route('/changes/<string:change_id>')
    class ChangeOne(Resource):
        @require('change:view')
        def get(self, change_id):
            """Plan, approvals, schedule and rollout progress per wave and device."""
            return changes.to_dict(changes.get(change_id))

    def _reason():
        b = request.get_json(silent=True)
        return b.get('reason') if isinstance(b, dict) else None

    @api.route('/changes/<string:change_id>/approve')
    class ChangeApprove(Resource):
        @require('change:approve')
        def post(self, change_id):
            """Approve the current plan hash {plan_hash, comment}; the author can't."""
            b = request.get_json(silent=True) or {}
            return changes.approve(change_id, principal(), b.get('plan_hash'), b.get('comment'))

    @api.route('/changes/<string:change_id>/reject')
    class ChangeReject(Resource):
        @require('change:approve')
        def post(self, change_id):
            """Reject with a reason."""
            return changes.reject(change_id, principal(), _reason())

    @api.route('/changes/<string:change_id>/plan')
    class ChangePlan(Resource):
        @require('change:author')
        def post(self, change_id):
            """Re-plan against the fleets' current state (earlier approvals stop counting if the hash changes)."""
            return changes.replan(change_id, principal())

    @api.route('/changes/<string:change_id>/apply')
    class ChangeApply(Resource):
        @require('change:apply')
        def post(self, change_id):
            """Apply now inside an open window, or as an approved emergency."""
            return changes.apply_now(change_id, principal())

    @api.route('/changes/<string:change_id>/cancel')
    class ChangeCancel(Resource):
        @require('change:author')
        def post(self, change_id):
            """Cancel before it applies."""
            return changes.cancel(change_id, principal(), _reason())

    @api.route('/changes/<string:change_id>/rollback')
    class ChangeRollback(Resource):
        @require('change:apply')
        def post(self, change_id):
            """Roll back: re-pin the previous release and restore env values the change set."""
            return changes.request_rollback(change_id, principal(), _reason())

    @api.route('/change-control')
    class ChangeControl(Resource):
        @require('change:view')
        def get(self):
            """Mode, policy, windows, freezes and the next open window."""
            cfg = comp.settings().change_control
            return dict(cfg.to_dict(), schedule=cc.Schedule(cfg).describe(clock()))

    @api.route('/fleets/<path:fleet_ref>/site-changes')
    class SiteChanges(Resource):
        @require('change:author')
        def post(self, fleet_ref):
            """Send a site change set to the fleet's devices in waves
            {title, ticket, change: <site change set>, waves, soak_min, timeout_min, window, signature}."""
            b = body()
            f = fleet.fleet(fleet_ref)
            item = {'op': 'site.change', 'fleet': f['slug'], 'change': b.get('change')}
            for k in ('waves', 'soak_min', 'timeout_min'):
                if k in b:
                    item[k] = b[k]
            wrapped = {k: b[k] for k in ('title', 'ticket', 'window', 'emergency', 'reason', 'draft') if k in b}
            wrapped['items'] = [item]
            change, created = changes.create(wrapped, principal(), request.headers.get('Idempotency-Key'),
                                             signed_body=b)
            return change, 201 if created else 200

    # ── drift and server ──
    @api.route('/drift')
    class Drift(Resource):
        @require('fleet:view')
        def get(self):
            """Devices off their target release; target pins and env vars changed outside the API."""
            items = fleet.drift(request.args.get('fleet'))
            return {'drift': items, 'count': len(items)}

    @api.route('/server/health')
    class ServerHealth(Resource):
        @require('fleet:view')
        def get(self):
            """openBalena API, registry, VPN, haproxy backlog, builder disk."""
            return server_health(comp.balena(), comp.settings().raw, runner, session=http_session)

    @api.route('/server/operations')
    class ServerOperations(Resource):
        @require('system:admin')
        def post(self):
            """registry_prune {dry_run}, builder_prune, haproxy_restart (gated, async)."""
            b = body()
            op = b.get('type')
            if op not in OPERATIONS:
                raise EnvError('/type', 'type must be one of %s' % ', '.join(OPERATIONS))
            params = b.get('params') or {}
            if not isinstance(params, dict):
                raise EnvError('/params', 'must be an object')
            gate('op.server.%s' % op)
            return server_ops.start(op, params, actor()), 202

    @api.route('/server/operations/<string:op_id>')
    class ServerOperationOne(Resource):
        @require('fleet:view')
        def get(self, op_id):
            """A server operation's state and output."""
            op = server_ops.get(op_id)
            if op is None:
                raise NotFound('operation %s' % op_id)
            return op

    @api.route('/ping')
    class Ping(Resource):
        @public
        def get(self):
            """Liveness."""
            return {'ok': True}

    @app.route(SIGNIN_PAGE)
    @public
    def signin():
        return Response(_SIGNIN_HTML, mimetype='text/html')

    if start_scheduler is None:
        start_scheduler = os.environ.get('FLEET_API_SCHEDULER', '1') != '0'
    if start_scheduler:
        _start_scheduler(changes, lambda: int(comp.settings()['rollout'].get('tick_s', 15)))
    return app


def _start_scheduler(changes: FleetChanges, interval: Callable[[], int]):
    def loop():
        while True:
            try:
                changes.tick()
            except Exception:
                log.exception('scheduler tick failed')
            time.sleep(max(1, interval()))
    t = threading.Thread(target=loop, name='fleet-scheduler', daemon=True)
    t.start()
    return t


_SIGNIN_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Fleet API sign-in</title>
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="font-family:sans-serif;max-width:22rem;margin:4rem auto;padding:0 1rem">
<h1 style="font-size:1.2rem">Fleet Control API</h1>
<form method="post" action="/fleet/auth/login/password">
<input type="hidden" name="next" id="next" value="/fleet/v1/auth/me">
<p><label>Username<br><input name="username" autocomplete="username" required></label></p>
<p><label>Password<br><input name="password" type="password" autocomplete="current-password" required></label></p>
<p><button type="submit">Sign in</button> <a href="/fleet/auth/login?method=sso">Use SSO</a></p>
</form>
<script>var n=new URLSearchParams(location.search).get('next');if(n&&n[0]==='/')document.getElementById('next').value=n;</script>
</body></html>"""


def static_auth(doc=None):
    """Tests and tools: an in-memory auth config."""
    return StaticConfigProvider.from_dict(doc or {}, {})
