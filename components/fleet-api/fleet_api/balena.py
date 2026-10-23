"""openBalena client: the balena REST/OData ("pine") API at <api>/v6, authenticated with an
API key held by this service. The key is sent as a bearer token and never returned or logged.

Writes follow what balena-sdk does (v22, the CLI this repo's builder ships):
  release finalize  PATCH /v6/release(<id>) {is_final: true}
  fleet pin         PATCH /v6/application(<id>) {should_be_running__release, should_track_latest_release: false}
  device pin        PATCH /v6/device(<id>) {is_pinned_on__release: <id> | null}
  restart service   POST /supervisor/v2/applications/<appId>/restart-service {deviceId, appId, data}
  reboot            POST /supervisor/v1/reboot {deviceId, data: {force}}
"""
import logging
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import quote

import requests

log = logging.getLogger('fleet_api')

DEVICE_SELECT = ('id,uuid,device_name,is_online,last_connectivity_event,api_heartbeat_state,status,'
                 'is_running__release,is_pinned_on__release,supervisor_version,os_version,belongs_to__application')
FLEET_SELECT = 'id,app_name,slug,should_be_running__release,should_track_latest_release'
RELEASE_SELECT = 'id,commit,status,is_final,semver,raw_version,created_at,end_timestamp,belongs_to__application'
RELEASE_IMAGES = ('release_image($expand=image($select=id,status,content_hash,image_size;'
                  '$expand=is_a_build_of__service($select=service_name)))')


class BalenaError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__('openBalena API %s: %s' % (status, message))
        self.status, self.message = status, message


def lit(value) -> str:
    """An OData literal."""
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if value is None:
        return 'null'
    if isinstance(value, int):
        return str(value)
    return "'%s'" % str(value).replace("'", "''")


def ref_id(value) -> Optional[int]:
    """A navigation property as returned unexpanded ({"__id": n}) or expanded ([{id}])."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value.get('__id', value.get('id'))
    if isinstance(value, list):
        return ref_id(value[0]) if value else None
    if isinstance(value, int):
        return value
    return None


def any_of(field: str, values: Iterable) -> str:
    vals = list(values)
    return '(' + ' or '.join('%s eq %s' % (field, lit(v)) for v in vals) + ')' if vals else 'false'


class BalenaClient:
    def __init__(self, api_url: str, api_key: str, verify=True, timeout_s: float = 30,
                 session: Optional[requests.Session] = None):
        self.api_url = api_url.rstrip('/')
        self._key = api_key
        self.verify = verify
        self.timeout = timeout_s
        self.http = session or requests.Session()

    def __repr__(self):
        return '<BalenaClient %s>' % self.api_url

    # ── transport ──
    def _request(self, method: str, path: str, params=None, body=None, timeout=None) -> Any:
        if not self._key:
            raise BalenaError(0, 'no balena API key configured')
        url = self.api_url + path
        try:
            r = self.http.request(method, url, params=params, json=body, timeout=timeout or self.timeout,
                                  verify=self.verify, headers={'Authorization': 'Bearer ' + self._key})
        except requests.RequestException as e:
            raise BalenaError(0, '%s %s failed: %s' % (method, path, type(e).__name__))
        if r.status_code >= 400:
            raise BalenaError(r.status_code, (r.text or '')[:300])
        if not r.content:
            return None
        try:
            return r.json()
        except ValueError:
            return r.text

    def get(self, resource: str, filter: Optional[str] = None, select: Optional[str] = None,
            expand: Optional[str] = None, orderby: Optional[str] = None, top: Optional[int] = None) -> List[dict]:
        params = {}
        for k, v in (('$filter', filter), ('$select', select), ('$expand', expand), ('$orderby', orderby),
                     ('$top', top)):
            if v is not None:
                params[k] = str(v)
        body = self._request('GET', '/v6/%s' % resource, params=params)
        return (body or {}).get('d', []) if isinstance(body, dict) else []

    def patch(self, resource: str, body: dict, id: Optional[int] = None, filter: Optional[str] = None):
        path = '/v6/%s' % resource + ('(%d)' % id if id is not None else '')
        params = {'$filter': filter} if filter else None
        return self._request('PATCH', path, params=params, body=body)

    def post(self, resource: str, body: dict) -> dict:
        return self._request('POST', '/v6/%s' % resource, body=body) or {}

    def delete(self, resource: str, id: int):
        return self._request('DELETE', '/v6/%s(%d)' % (resource, id))

    def ping(self) -> bool:
        try:
            r = self.http.get(self.api_url + '/ping', timeout=self.timeout, verify=self.verify)
            return r.status_code == 200
        except requests.RequestException:
            return False

    # ── fleets ──
    def fleets(self) -> List[dict]:
        return self.get('application', select=FLEET_SELECT, orderby='slug asc')

    def fleet(self, ref: str) -> Optional[dict]:
        """By slug (admin/name), bare name, or numeric id."""
        ref = str(ref)
        if ref.isdigit():
            rows = self.get('application', filter='id eq %d' % int(ref), select=FLEET_SELECT)
        elif '/' in ref:
            rows = self.get('application', filter='slug eq %s' % lit(ref.lower()), select=FLEET_SELECT)
        else:
            rows = self.get('application', filter='app_name eq %s' % lit(ref), select=FLEET_SELECT)
        return rows[0] if len(rows) == 1 else None

    def pin_fleet(self, app_id: int, release_id: int):
        self.patch('application', {'should_be_running__release': release_id,
                                   'should_track_latest_release': False}, id=app_id)

    # ── releases ──
    def releases(self, app_id: int, top: int = 50, images: bool = True) -> List[dict]:
        return self.get('release', filter='belongs_to__application eq %d' % app_id, select=RELEASE_SELECT,
                        expand=RELEASE_IMAGES if images else None, orderby='created_at desc', top=top)

    def release(self, release_id: int) -> Optional[dict]:
        rows = self.get('release', filter='id eq %d' % int(release_id), select=RELEASE_SELECT)
        return rows[0] if rows else None

    def find_release(self, app_id: int, ref) -> Optional[dict]:
        """A release of this fleet by id or (full or short) commit."""
        ref = str(ref)
        if ref.isdigit():
            rows = self.get('release', filter='id eq %d and belongs_to__application eq %d' % (int(ref), app_id),
                            select=RELEASE_SELECT)
        else:
            rows = self.get('release', filter='belongs_to__application eq %d and startswith(commit,%s)'
                            % (app_id, lit(ref)), select=RELEASE_SELECT)
        return rows[0] if len(rows) == 1 else None

    def finalize_release(self, release_id: int):
        self.patch('release', {'is_final': True}, id=release_id)

    # ── devices ──
    def devices(self, app_id: int) -> List[dict]:
        return self.get('device', filter='belongs_to__application eq %d' % app_id, select=DEVICE_SELECT,
                        orderby='device_name asc')

    def device(self, uuid: str) -> Optional[dict]:
        uuid = str(uuid).lower()
        if len(uuid) in (32, 62):
            rows = self.get('device', filter='uuid eq %s' % lit(uuid), select=DEVICE_SELECT)
        else:
            rows = self.get('device', filter='startswith(uuid,%s)' % lit(uuid), select=DEVICE_SELECT)
        return rows[0] if len(rows) == 1 else None

    def pin_device(self, device_id: int, release_id: Optional[int]):
        self.patch('device', {'is_pinned_on__release': release_id}, id=device_id)

    # ── services and env vars ──
    def services(self, app_id: int) -> List[dict]:
        return self.get('service', filter='application eq %d' % app_id, select='id,service_name')

    def fleet_env(self, app_id: int) -> List[dict]:
        return self.get('application_environment_variable', filter='application eq %d' % app_id,
                        select='id,name,value')

    def service_env(self, service_ids: Iterable[int]) -> List[dict]:
        ids = list(service_ids)
        if not ids:
            return []
        return self.get('service_environment_variable', filter=any_of('service', ids), select='id,name,value,service')

    def device_env(self, device_id: int) -> List[dict]:
        return self.get('device_environment_variable', filter='device eq %d' % device_id, select='id,name,value')

    def service_installs(self, device_id: int) -> List[dict]:
        return self.get('service_install', filter='device eq %d' % device_id, select='id,installs__service')

    def device_service_env(self, install_ids: Iterable[int]) -> List[dict]:
        ids = list(install_ids)
        if not ids:
            return []
        return self.get('device_service_environment_variable', filter=any_of('service_install', ids),
                        select='id,name,value,service_install')

    def set_var(self, resource: str, parent_field: str, parent_id: int, name: str, value: str,
                existing: Optional[dict]):
        if existing is not None:
            self.patch(resource, {'value': value}, id=existing['id'])
        else:
            self.post(resource, {parent_field: parent_id, 'name': name, 'value': value})

    # ── supervisor actions through the API ──
    def supervisor(self, path: str, body: dict, timeout=None) -> Any:
        return self._request('POST', '/supervisor' + path, body=body, timeout=timeout or max(self.timeout, 60))

    def restart_service(self, device_id: int, app_id: int, service_name: str):
        return self.supervisor('/v2/applications/%d/restart-service' % app_id,
                               {'deviceId': device_id, 'appId': app_id,
                                'data': {'appId': app_id, 'serviceName': service_name}})

    def reboot(self, device_id: int, force: bool = False):
        return self.supervisor('/v1/reboot', {'deviceId': device_id, 'data': {'force': bool(force)}})


def fleet_path(slug: str) -> str:
    return quote(slug, safe='')


def normalize_device(d: dict, commits: Optional[Dict[int, str]] = None, fleet_target: Optional[int] = None) -> dict:
    commits = commits or {}
    running = ref_id(d.get('is_running__release'))
    pinned = ref_id(d.get('is_pinned_on__release'))
    target = pinned or fleet_target
    return {
        'id': d.get('id'), 'uuid': d.get('uuid'), 'name': d.get('device_name'),
        'online': bool(d.get('is_online')), 'last_seen': d.get('last_connectivity_event'),
        'api_heartbeat_state': d.get('api_heartbeat_state'), 'status': d.get('status'),
        'release_id': running, 'release': commits.get(running) if running else None,
        'target_release_id': target, 'target_release': commits.get(target) if target else None,
        'pinned_release_id': pinned, 'supervisor_version': d.get('supervisor_version'),
        'os_version': d.get('os_version'), 'fleet_id': ref_id(d.get('belongs_to__application')),
    }


def normalize_release(r: dict) -> dict:
    images = []
    for ri in r.get('release_image') or []:
        img = (ri.get('image') or [{}])
        img = img[0] if isinstance(img, list) and img else (img if isinstance(img, dict) else {})
        svc = img.get('is_a_build_of__service')
        svc = svc[0] if isinstance(svc, list) and svc else (svc if isinstance(svc, dict) else {})
        images.append({'id': img.get('id'), 'service': svc.get('service_name'), 'status': img.get('status'),
                       'size': img.get('image_size'), 'content_hash': img.get('content_hash')})
    return {'kind': 'release', 'id': r.get('id'), 'commit': r.get('commit'), 'status': r.get('status'),
            'finalized': bool(r.get('is_final')), 'version': r.get('semver') or r.get('raw_version'),
            'created_at': r.get('created_at'), 'finished_at': r.get('end_timestamp'),
            'images': sorted(images, key=lambda i: i.get('service') or '')}
