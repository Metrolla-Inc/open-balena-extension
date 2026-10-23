"""Flashable images through this repo's imagemaker (components/imagemaker, :8090).

  POST /api/build {fleet, deviceType, version, network, connectivity, ssid, key} -> {id}
  GET  /api/status/<id> -> {status: building|done|error, message, error, name}
  GET  /download/<id>   -> the .img.gz
  GET  /api/fleets, /api/versions?deviceType=  (defaults for device type and OS version)

The imagemaker keeps jobs in memory, so its restart loses in-flight and finished builds; the
image row here then reports failed. The Wi-Fi key is passed through and never stored.
"""
from typing import Optional

import requests

from .models import Image, iso, new_id

STATUS = {'building': 'building', 'done': 'ready', 'error': 'failed'}


class ImageError(Exception):
    def __init__(self, status: int, message: str, pointer: str = ''):
        super().__init__(message)
        self.status, self.message, self.pointer = status, message, pointer


def image_to_dict(img: Image) -> dict:
    return {'id': img.id, 'fleet': img.fleet, 'status': img.status, 'params': img.params, 'name': img.name,
            'error': img.error, 'created_by': img.created_by, 'created_at': iso(img.created_at),
            'download_url': '/fleet/v1/images/%s/download' % img.id if img.status == 'ready' else None}


class ImagemakerClient:
    def __init__(self, url: str, token: str = '', timeout_s: int = 60, session=None):
        self.url = url.rstrip('/')
        self._token = token
        self.timeout = timeout_s
        self.http = session or requests.Session()

    def _headers(self):
        return {'x-imagemaker-token': self._token} if self._token else {}

    def _get(self, path, **kw):
        try:
            r = self.http.get(self.url + path, headers=self._headers(), timeout=self.timeout, **kw)
        except requests.RequestException as e:
            raise ImageError(502, 'imagemaker unreachable: %s' % type(e).__name__)
        return r

    def fleets(self):
        r = self._get('/api/fleets')
        return r.json() if r.status_code == 200 else []

    def versions(self, device_type: str):
        r = self._get('/api/versions', params={'deviceType': device_type})
        return r.json() if r.status_code == 200 else []

    def build(self, body: dict) -> str:
        try:
            r = self.http.post(self.url + '/api/build', json=body, headers=self._headers(), timeout=self.timeout)
        except requests.RequestException as e:
            raise ImageError(502, 'imagemaker unreachable: %s' % type(e).__name__)
        if r.status_code == 429:
            raise ImageError(429, 'imagemaker is already running two builds; retry shortly')
        if r.status_code != 200:
            raise ImageError(502, 'imagemaker answered %d: %s' % (r.status_code, r.text[:200]))
        return r.json()['id']

    def status(self, job_id: str) -> Optional[dict]:
        r = self._get('/api/status/%s' % job_id)
        if r.status_code == 404:
            return None
        return r.json() if r.status_code == 200 else None

    def download(self, job_id: str):
        return self._get('/download/%s' % job_id, stream=True)


class ImageService:
    def __init__(self, session_factory, client):
        self.session_factory = session_factory
        self.client = client

    def create(self, fleet: dict, body: dict, actor: str) -> dict:
        cfg = body.get('config') or {}
        if not isinstance(cfg, dict):
            raise ImageError(422, 'config must be an object', '/config')
        device_type = body.get('device_type')
        if not device_type:
            match = [f for f in self.client().fleets() if f.get('slug') == fleet['slug']]
            device_type = match[0].get('deviceType') if match else None
        if not device_type:
            raise ImageError(422, 'device_type is required (the imagemaker does not know this fleet)', '/device_type')
        version = body.get('os_version')
        if not version:
            versions = self.client().versions(device_type)
            version = versions[0] if versions else None
        if not version:
            raise ImageError(422, 'os_version is required (no versions listed for %s)' % device_type, '/os_version')
        network = 'wifi' if cfg.get('network') == 'wifi' else 'ethernet'
        connectivity = 'internet' if cfg.get('connectivity', 'internet') == 'internet' else 'lan'
        job = self.client().build({'fleet': fleet['slug'], 'deviceType': device_type, 'version': str(version),
                                   'network': network, 'connectivity': connectivity,
                                   'ssid': cfg.get('wifi_ssid') or '', 'key': cfg.get('wifi_key') or ''})
        params = {'device_type': device_type, 'os_version': str(version), 'network': network,
                  'connectivity': connectivity, 'wifi_ssid': cfg.get('wifi_ssid') or None}
        db = self.session_factory()
        try:
            img = Image(id=new_id('img'), fleet=fleet['slug'], params=params, imagemaker_id=job, status='building',
                        created_by=actor)
            db.add(img)
            db.commit()
            return image_to_dict(img)
        finally:
            db.close()

    def get(self, image_id: str) -> Optional[dict]:
        db = self.session_factory()
        try:
            img = db.get(Image, image_id)
            if img is None:
                return None
            if img.status == 'building':
                st = self.client().status(img.imagemaker_id)
                if st is None:
                    img.status, img.error = 'failed', 'the imagemaker no longer knows this build (restarted?)'
                else:
                    img.status = STATUS.get(st.get('status'), 'building')
                    img.error = st.get('error')
                    img.name = st.get('name')
                db.commit()
            return image_to_dict(img)
        finally:
            db.close()

    def imagemaker_id(self, image_id: str) -> Optional[str]:
        db = self.session_factory()
        try:
            img = db.get(Image, image_id)
            return img.imagemaker_id if img and img.status == 'ready' else None
        finally:
            db.close()
