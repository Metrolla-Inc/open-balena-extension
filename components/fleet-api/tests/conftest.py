import copy
import logging
import os
import subprocess
import sys
import uuid

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from fakes import FakeBalena, FakeClock, FakeImagemaker, FakeRunner, FakeVpn, HookSink  # noqa: E402
from fleet_api.app import create_app, static_auth  # noqa: E402
from fleet_api.jobs import InlineJobs  # noqa: E402
from fleet_api.models import make_engine  # noqa: E402
from fleet_api.settings import SettingsProvider  # noqa: E402

logging.getLogger('werkzeug').setLevel(logging.WARNING)

FLEET = 'admin/bellagio-retail'
FLEET_SEG = 'admin%2Fbellagio-retail'


def _git(cwd, *args):
    subprocess.run(['git', '-c', 'user.email=t@example.com', '-c', 'user.name=t', *args], cwd=cwd, check=True,
                   capture_output=True)


@pytest.fixture(scope='session')
def repos(tmp_path_factory):
    """A local 'remote' git repo: balena-fleets with bellagio-retail/ tagged v2.2.0."""
    base = tmp_path_factory.mktemp('repos')
    repo = base / 'balena-fleets'
    (repo / 'bellagio-retail').mkdir(parents=True)
    (repo / 'bellagio-retail' / 'docker-compose.yml').write_text('services:\n  edgeapi:\n    build: ./edgeapi\n')
    (repo / 'bellagio-retail' / 'edgeapi').mkdir()
    (repo / 'bellagio-retail' / 'edgeapi' / 'Dockerfile').write_text('FROM alpine\n')
    (repo / 'bellagio-retail' / '.dockerignore').write_text('notes.txt\n')
    (repo / 'bellagio-retail' / 'notes.txt').write_text('not for the builder\n')
    _git(repo, 'init', '-q', '-b', 'main')
    _git(repo, 'add', '.')
    _git(repo, 'commit', '-q', '-m', 'init')
    _git(repo, 'tag', 'v2.2.0')
    return str(base) + '/'


class World:
    """The fakes plus a running Fleet API app."""

    def __init__(self, tmp_path, repos):
        self.tmp = tmp_path
        self.key = 'test-' + uuid.uuid4().hex
        self.keyfile = tmp_path / 'balena_api_key'
        self.keyfile.write_text(self.key + '\n')
        self.imtoken = 'im-' + uuid.uuid4().hex
        (tmp_path / 'imagemaker_token').write_text(self.imtoken)
        self.clock = FakeClock()
        self.balena = FakeBalena(self.key, self.clock)
        self.vpn = FakeVpn(self.key)
        self.imagemaker = FakeImagemaker(self.imtoken)
        self.hooks = HookSink()
        self.runner = FakeRunner()
        self.jobs = InlineJobs()
        self.repos = repos
        self.app = None
        self.doc = {
            'balena': {'api_url': self.balena.url, 'api_key_file': str(self.keyfile), 'api_key_env': '',
                       'verify': False},
            'builder': {'url': self.balena.url, 'release_wait_s': 0},
            'git': {'repo_base': repos, 'allowed_repos': ['balena-fleets']},
            'reach': {'proxy_host': '127.0.0.1', 'proxy_port': self.vpn.port, 'timeout_s': 3},
            'imagemaker': {'url': self.imagemaker.url, 'token_file': str(tmp_path / 'imagemaker_token')},
            'server': {'registry_url': self.balena.url + '/ping', 'builder_url': self.balena.url + '/ping',
                       'haproxy_health_url': self.balena.url + '/ping'},
            'rollout': {'update_timeout_min': 30, 'verify_settle_s': 0},
            'webhooks': [{'url': self.hooks.url, 'events': ['fleet.change.*']}],
            'change_control': {},
        }
        # A fleet on release r1 with four online devices.
        self.fleet = self.balena.fleet(FLEET)
        self.r1 = self.balena.release(self.fleet)
        self.balena.pin_fleet(self.fleet, self.r1)
        self.devices = [self.balena.device(self.fleet, n, self.r1) for n in
                        ('wandering-mountain', 'quiet-river', 'amber-field', 'stone-bridge')]
        for d in self.devices:
            self.vpn.add(d['uuid'])

    def start(self, change_control=None, auth=None, **doc_overrides):
        doc = copy.deepcopy(self.doc)
        if change_control is not None:
            doc['change_control'] = change_control
        doc.update(doc_overrides)
        self.settings = SettingsProvider(doc=doc)
        self.app = create_app(settings=self.settings, auth_provider=static_auth(auth), engine=make_engine('sqlite://'),
                              clock=self.clock, jobs=self.jobs, runner=self.runner, start_scheduler=False,
                              data_dir=str(self.tmp / 'data'), sleep=lambda s: None)
        self.client = self.app.test_client()
        self.headers = as_('root') if auth else {}
        self.ext = self.app.extensions['fleet_api']
        return self

    def set_change_control(self, cc):
        doc = copy.deepcopy(self.doc)
        doc['change_control'] = cc
        self.settings.replace(doc)

    @property
    def changes(self):
        return self.ext['changes']

    def tick(self, minutes=0, simulate=True, stuck=()):
        if minutes:
            self.clock.advance(minutes=minutes)
        if simulate:
            self.balena.simulate_updates(stuck=stuck)
        self.changes.tick()

    def change(self, cid):
        r = self.client.get('/fleet/v1/changes/%s' % cid, headers=self.headers)
        assert r.status_code == 200, r.json
        return r.json

    def device(self, name):
        return next(d for d in self.devices if d['device_name'] == name)

    def edge(self, name):
        return self.vpn.devices[self.device(name)['uuid']]

    def close(self):
        for s in (self.balena, self.vpn, self.imagemaker, self.hooks):
            s.close()


@pytest.fixture
def world(tmp_path, repos):
    w = World(tmp_path, repos)
    yield w
    w.close()


TOKENS = {name: 'mtl_st_%s_%s' % (name, uuid.uuid4().hex) for name in
          ('ci', 'amy', 'bob', 'cal', 'viewer', 'rm', 'root')}
# amy and bob approve with every fleet permission; cal holds change:approve but no fleet permission.
ROLES = {'ci': 'change_pipeline', 'amy': 'site_admin', 'bob': 'site_admin', 'cal': 'change_approver',
         'viewer': 'fleet_viewer', 'rm': 'fleet_release_manager', 'root': 'site_admin'}


def auth_on():
    from metrolla_auth import hash_token
    return {'auth': {'enabled': True, 'static_tokens': {'enabled': True, 'tokens': [
        {'user_id': n, 'token_sha256': hash_token(t), 'role': ROLES[n]} for n, t in TOKENS.items()]}}}


def as_(name):
    return {'Authorization': 'Bearer ' + TOKENS[name], 'X-Metrolla-User': name}
