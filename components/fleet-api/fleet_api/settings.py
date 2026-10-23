"""Service config: /etc/fleet-api/fleet-api.yaml (FLEET_API_CONFIG).

Secrets are never in this file: the balena API key, git token, imagemaker token, device token
and webhook secrets are read from files (or env) named here. The file is re-read when its
mtime changes, so change_control windows and freezes can be edited without a restart.
"""
import copy
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

import yaml

from metrolla_change_control import ChangeControlConfig, ChangeControlConfigError, parse_change_control

log = logging.getLogger('fleet_api')

_VAR = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}')

DEFAULT_SECRET_PATTERNS = ['TOKEN', 'SECRET', 'PASSWORD', 'PASSWD', 'KEY', 'CREDENTIAL', 'PRIVATE', 'DSN']

DEFAULTS: Dict[str, Any] = {
    'balena': {
        'api_url': 'https://api.${DNS_TLD}',
        'api_key_file': '/run/secrets/balena_api_key',
        'api_key_env': 'BALENA_API_KEY',
        'verify': '/certs/root-ca.pem', # true | false | path to the openBalena CA
        'timeout_s': 30,
    },
    'builder': {
        'mode': 'http',                 # http (POST /v3/build like balena push) | cli (balena push)
        'url': 'http://builder:80',     # in-network: no haproxy timeout in the way
        'timeout_s': 3600,
        'cli': 'balena',
        'release_wait_s': 300,
    },
    'git': {
        'repo_base': 'https://github.com/Metrolla-Inc/',
        'allowed_repos': [],            # empty = any repo under repo_base
        'allow_urls': False,            # accept full URLs in {git: {repo}}
        'token_file': '',
        'ssh_key_file': '',
        'timeout_s': 300,
    },
    'reach': {
        'mode': 'vpn_proxy',            # vpn_proxy (CONNECT <uuid>.balena:80) | connectivity
        'proxy_host': 'vpn',
        'proxy_port': 3128,
        'proxy_tls': False,             # true for tunnel.<tld>:443 through haproxy
        'proxy_user': 'root',
        'device_port': 80,
        'timeout_s': 10,
        'health_path': '/api/v1/health',
        'device_token_file': '',        # bearer for device EdgeAPI when its sign-in is on
        'device_user': '',              # X-Metrolla-User for a static device token
    },
    'imagemaker': {
        'url': 'http://host.docker.internal:8090',
        'token_file': '/run/secrets/imagemaker_token',
        'timeout_s': 60,
    },
    'server': {
        'docker': 'docker',
        'containers': {
            'builder_dind': 'open-balena-builder-dind-1',
            'haproxy': 'open-balena-haproxy-1',
            'registry': 'open-balena-registry-1',
        },
        'registry_url': 'http://registry:80/v2/',
        'builder_url': 'http://builder:80/',
        'haproxy_health_url': 'http://haproxy:80/health',
        'haproxy_stats_url': '',        # e.g. https://haproxy:1936/metrics;csv
        'haproxy_stats_auth_file': '',  # "user:password"
        'registry_config': '/etc/docker/registry/config.yml',
        'builder_disk_warn_pct': 80,
        'builder_disk_fail_pct': 90,
        'command_timeout_s': 900,
    },
    'env': {'secret_patterns': DEFAULT_SECRET_PATTERNS},
    'rollout': {
        'update_timeout_min': 30,       # a wave's devices must run the release within this
        'site_change_timeout_min': 1440,
        'tick_s': 15,
        'verify_settle_s': 60,          # health must hold this long after the last item before applied
    },
    'webhooks': [],                     # [{url, events: [glob], secret_file}]
    'change_control': {},
}


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, Mapping) and isinstance(out.get(k), Mapping):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _expand(value, environ):
    if isinstance(value, str):
        return _VAR.sub(lambda m: str(environ.get(m.group(1), m.group(0))), value)
    if isinstance(value, Mapping):
        return {k: _expand(v, environ) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v, environ) for v in value]
    return value


def read_secret(path: str = '', env: str = '', environ: Optional[Mapping] = None) -> str:
    environ = os.environ if environ is None else environ
    if env and environ.get(env):
        return environ[env].strip()
    if path:
        try:
            with open(path) as fh:
                return fh.read().strip()
        except OSError:
            return ''
    return ''


@dataclass
class Settings:
    raw: Dict[str, Any]
    change_control: ChangeControlConfig
    errors: List[dict] = field(default_factory=list)

    def __getitem__(self, key):
        return self.raw[key]

    def get(self, key, default=None):
        return self.raw.get(key, default)


def build_settings(doc: Optional[Mapping], environ: Optional[Mapping] = None) -> Settings:
    environ = os.environ if environ is None else environ
    raw = _expand(_merge(DEFAULTS, doc or {}), environ)
    cc = parse_change_control(raw.get('change_control') or {})
    return Settings(raw=raw, change_control=cc)


class SettingsProvider:
    """Settings from a YAML file, reloaded on mtime change. An invalid edit keeps the last good
    settings (logged); an invalid file at start refuses to start."""

    def __init__(self, path: Optional[str] = None, doc: Optional[Mapping] = None, environ: Optional[Mapping] = None):
        self.path = path
        self.environ = os.environ if environ is None else environ
        self._lock = threading.Lock()
        self._mtime = None
        if path:
            self._settings = self._load(first=True)
        else:
            self._settings = build_settings(doc, self.environ)

    def _load(self, first=False) -> Settings:
        try:
            with open(self.path) as fh:
                doc = yaml.safe_load(fh) or {}
            self._mtime = os.stat(self.path).st_mtime
        except FileNotFoundError:
            self._mtime = None
            return build_settings({}, self.environ)
        try:
            return build_settings(doc, self.environ)
        except ChangeControlConfigError as e:
            if first:
                raise
            log.error('%s is invalid, keeping the previous settings: %s', self.path, e)
            return self._settings

    def current(self) -> Settings:
        if not self.path:
            return self._settings
        try:
            mtime = os.stat(self.path).st_mtime
        except OSError:
            mtime = None
        if mtime != self._mtime:
            with self._lock:
                if mtime != self._mtime:
                    self._settings = self._load()
        return self._settings

    def replace(self, doc: Mapping):
        """Tests: swap the in-memory settings."""
        self._settings = build_settings(doc, self.environ)
