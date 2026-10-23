"""DeviceReach: how the Fleet API talks to a device's EdgeAPI and judges its health.

VpnProxyReach uses openBalena's VPN service proxy, the path `balena device tunnel` takes
(balena-cli src/utils/tunnel.ts): open a connection to the proxy, send
  CONNECT <uuid>.balena:<port> HTTP/1.0
  Proxy-Authorization: Basic base64(<user>:<balena API key>)
and on "200" speak plain HTTP to the device's port through the tunnel. The CLI reaches the
proxy as TLS to tunnel.<tld>:443 (haproxy SNI -> vpn:3128); inside the openBalena network this
service can dial vpn:3128 directly (reach.proxy_host/proxy_port, proxy_tls false).
ASSUMPTIONS TO VERIFY ON .183: the vpn container's proxy listens on 3128 inside the network,
it accepts the API key as the Basic password with user "root" (the CLI sends the username
for user API keys), and the device EdgeAPI answers on port 80 over the VPN.

ConnectivityReach needs no path to the device: it judges health from what the openBalena API
already knows (is_online, api_heartbeat_state, last_connectivity_event). It is the fallback
whenever the proxy path fails, and the only mode with reach.mode: connectivity.
"""
import base64
import http.client
import json
import socket
import ssl
from dataclasses import dataclass
from typing import Any, Optional, Tuple

OK, DEGRADED, FAILED, UNREACHABLE, UNKNOWN = 'ok', 'degraded', 'failed', 'unreachable', 'unknown'
RANK = {OK: 0, DEGRADED: 1, FAILED: 2, UNREACHABLE: 2}

_SITE_STATUS = {'ok': OK, 'healthy': OK, 'green': OK, 'pass': OK, 'up': OK,
                'degraded': DEGRADED, 'yellow': DEGRADED, 'warn': DEGRADED, 'warning': DEGRADED,
                'down': FAILED, 'red': FAILED, 'failed': FAILED, 'fail': FAILED, 'error': FAILED}


class ReachError(Exception):
    pass


@dataclass
class DeviceHealth:
    status: str
    source: str                         # edgeapi | connectivity
    detail: Any = None
    online: Optional[bool] = None

    @property
    def rank(self) -> Optional[int]:
        return RANK.get(self.status)

    def to_dict(self) -> dict:
        return {'status': self.status, 'source': self.source, 'detail': self.detail}


def worse(now: 'DeviceHealth', baseline: Optional[dict]) -> bool:
    """True when `now` ranks below the baseline. Unknown on either side never counts."""
    if baseline is None or now.rank is None:
        return False
    base_rank = RANK.get(baseline.get('status'))
    return base_rank is not None and now.rank > base_rank


class ConnectivityReach:
    mode = 'connectivity'

    def health(self, device: dict) -> DeviceHealth:
        online = bool(device.get('online'))
        if not online:
            return DeviceHealth(FAILED, 'connectivity', 'offline since %s' % (device.get('last_seen') or '?'), online)
        hb = (device.get('api_heartbeat_state') or '').lower()
        if hb in ('timeout', 'offline'):
            return DeviceHealth(DEGRADED, 'connectivity', 'API heartbeat %s' % hb, online)
        return DeviceHealth(OK, 'connectivity', 'online', online)

    def request(self, device: dict, method: str, path: str, body=None, headers=None) -> Tuple[int, Any]:
        raise ReachError('reach.mode is connectivity: no path to the device EdgeAPI')


class _TunnelConnection(http.client.HTTPConnection):
    """HTTP over a socket opened by CONNECT through the VPN proxy."""

    def __init__(self, sock_factory, timeout):
        super().__init__('device', timeout=timeout)
        self._sock_factory = sock_factory

    def connect(self):
        self.sock = self._sock_factory()


class VpnProxyReach:
    mode = 'vpn_proxy'

    def __init__(self, proxy_host: str, proxy_port: int, api_key: str, proxy_user: str = 'root',
                 proxy_tls: bool = False, device_port: int = 80, timeout_s: float = 10,
                 health_path: str = '/api/v1/health', device_token: str = '', device_user: str = '',
                 fallback: Optional[ConnectivityReach] = None, tls_verify=True):
        self.proxy_host, self.proxy_port = proxy_host, proxy_port
        self._auth = base64.b64encode(('%s:%s' % (proxy_user, api_key)).encode()).decode()
        self.proxy_tls = proxy_tls
        self.device_port = device_port
        self.timeout_s = timeout_s
        self.health_path = health_path
        self._device_token = device_token
        self.device_user = device_user
        self.fallback = fallback or ConnectivityReach()
        self.tls_verify = tls_verify

    def _open(self, uuid: str) -> socket.socket:
        try:
            raw = socket.create_connection((self.proxy_host, self.proxy_port), timeout=self.timeout_s)
        except OSError as e:
            raise ReachError('VPN proxy %s:%s unreachable: %s' % (self.proxy_host, self.proxy_port, e))
        sock = raw
        try:
            if self.proxy_tls:
                ctx = ssl.create_default_context()
                if self.tls_verify is False:
                    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
                elif isinstance(self.tls_verify, str):
                    ctx.load_verify_locations(self.tls_verify)
                sock = ctx.wrap_socket(raw, server_hostname=self.proxy_host)
            sock.sendall(('CONNECT %s.balena:%d HTTP/1.0\r\nProxy-Authorization: Basic %s\r\n\r\n'
                          % (uuid, self.device_port, self._auth)).encode('ascii'))
            head = b''
            while b'\r\n\r\n' not in head:
                chunk = sock.recv(1)
                if not chunk:
                    raise ReachError('VPN proxy closed the connection during CONNECT')
                head += chunk
                if len(head) > 8192:
                    raise ReachError('VPN proxy sent an oversized CONNECT response')
            status_line = head.split(b'\r\n', 1)[0].decode('latin-1')
            parts = status_line.split(' ', 2)
            if len(parts) < 2 or parts[1] != '200':
                raise ReachError('VPN proxy refused CONNECT to %s: %s' % (uuid[:7], status_line))
            return sock
        except (OSError, ssl.SSLError) as e:
            sock.close()
            raise ReachError('VPN proxy CONNECT failed: %s' % e)
        except ReachError:
            sock.close()
            raise

    def request(self, device: dict, method: str, path: str, body=None, headers=None) -> Tuple[int, Any]:
        uuid = device['uuid']
        conn = _TunnelConnection(lambda: self._open(uuid), self.timeout_s)
        hdrs = {'Accept': 'application/json', 'Host': 'localhost', 'Connection': 'close'}
        if self._device_token:
            hdrs['Authorization'] = 'Bearer ' + self._device_token
            if self.device_user:
                hdrs['X-Metrolla-User'] = self.device_user
        hdrs.update(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            hdrs['Content-Type'] = 'application/json'
        try:
            conn.request(method, path, body=data, headers=hdrs)
            resp = conn.getresponse()
            raw = resp.read(4 * 1024 * 1024)
        except ReachError:
            raise
        except (OSError, http.client.HTTPException) as e:
            raise ReachError('device %s: %s' % (uuid[:7], e))
        finally:
            conn.close()
        try:
            payload = json.loads(raw) if raw else None
        except ValueError:
            payload = raw.decode('utf-8', 'replace')[:500]
        return resp.status, payload

    def health(self, device: dict) -> DeviceHealth:
        if not device.get('online'):
            return self.fallback.health(device)
        try:
            status, body = self.request(device, 'GET', self.health_path)
        except ReachError as e:
            h = self.fallback.health(device)
            h.detail = '%s; EdgeAPI not reachable (%s)' % (h.detail, e)
            return h
        if status != 200 or not isinstance(body, dict):
            h = self.fallback.health(device)
            h.detail = '%s; EdgeAPI health answered %s' % (h.detail, status)
            return h
        site = str(body.get('site_status') or body.get('status') or '').lower()
        return DeviceHealth(_SITE_STATUS.get(site, UNKNOWN), 'edgeapi',
                            {'site_status': body.get('site_status') or body.get('status'),
                             'detection_ok': body.get('detection_ok')}, True)


def make_reach(cfg: dict, api_key: str, device_token: str = '', tls_verify=True):
    fallback = ConnectivityReach()
    if cfg.get('mode') == 'connectivity':
        return fallback
    return VpnProxyReach(cfg['proxy_host'], int(cfg['proxy_port']), api_key, proxy_user=cfg.get('proxy_user', 'root'),
                         proxy_tls=bool(cfg.get('proxy_tls')), device_port=int(cfg.get('device_port', 80)),
                         timeout_s=float(cfg.get('timeout_s', 10)), health_path=cfg.get('health_path', '/api/v1/health'),
                         device_token=device_token, device_user=cfg.get('device_user', ''), fallback=fallback,
                         tls_verify=tls_verify)
