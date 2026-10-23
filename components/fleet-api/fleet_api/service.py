"""Fleet operations behind the routes: fleets, releases, devices, env vars, pinning, device
operations and drift. Change sets (changes.py) call the same methods.
"""
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Mapping, Optional

from sqlalchemy import select

from .balena import BalenaClient, BalenaError, normalize_device, normalize_release, ref_id
from .builds import build_to_dict
from .env import EnvError, EnvStore, is_secret, masked, value_sha256
from .models import AppliedEnv, AppliedTarget, Build, utcnow

log = logging.getLogger('fleet_api')


class NotFound(Exception):
    def __init__(self, what: str):
        super().__init__(what)
        self.what = what


class FleetService:
    def __init__(self, session_factory, balena: Callable[[], BalenaClient], reach: Callable,
                 settings: Callable):
        self.session_factory = session_factory
        self.balena = balena
        self.reach = reach
        self.settings = settings

    @property
    def patterns(self):
        return self.settings()['env']['secret_patterns']

    # ── lookups ──
    def fleet(self, ref: str) -> dict:
        f = self.balena().fleet(ref)
        if f is None:
            raise NotFound('fleet %s' % ref)
        return f

    def device(self, uuid: str) -> dict:
        d = self.balena().device(uuid)
        if d is None:
            raise NotFound('device %s' % uuid)
        return d

    def fleet_of(self, device: dict) -> dict:
        app_id = ref_id(device.get('belongs_to__application'))
        f = self.balena().fleet(str(app_id))
        if f is None:
            raise NotFound('fleet of device %s' % device.get('uuid'))
        return f

    @staticmethod
    def target_id(fleet: dict) -> Optional[int]:
        return ref_id(fleet.get('should_be_running__release'))

    def _commits(self, app_id: int) -> Dict[int, str]:
        return {r['id']: r['commit'] for r in self.balena().releases(app_id, top=200, images=False)}

    # ── fleets ──
    def list_fleets(self) -> List[dict]:
        b = self.balena()
        out = []
        for f in b.fleets():
            target = self.target_id(f)
            devices = b.devices(f['id'])
            rel = b.release(target) if target else None
            out.append({'id': f['id'], 'slug': f['slug'], 'name': f.get('app_name'),
                        'target_release_id': target, 'target_release': rel.get('commit') if rel else None,
                        'tracks_latest': bool(f.get('should_track_latest_release')),
                        'device_count': len(devices), 'online_count': sum(1 for d in devices if d.get('is_online'))})
        return out

    def list_releases(self, fleet: dict, top: int = 50) -> List[dict]:
        rows = [normalize_release(r) for r in self.balena().releases(fleet['id'], top=top)]
        target = self.target_id(fleet)
        for r in rows:
            r['is_target'] = r['id'] == target
        db = self.session_factory()
        try:
            builds = db.execute(select(Build).where(Build.fleet == fleet['slug']).order_by(Build.created_at.desc())
                                .limit(20)).scalars().all()
        finally:
            db.close()
        by_release = {b.release_id: b.id for b in builds if b.release_id}
        for r in rows:
            if r['id'] in by_release:
                r['build_id'] = by_release[r['id']]
        return [build_to_dict(b) for b in builds] + rows

    def list_devices(self, fleet: dict, with_health: bool = True) -> List[dict]:
        b = self.balena()
        commits = self._commits(fleet['id'])
        target = self.target_id(fleet)
        devices = [normalize_device(d, commits, target) for d in b.devices(fleet['id'])]
        if with_health and devices:
            reach = self.reach()
            with ThreadPoolExecutor(max_workers=min(8, len(devices))) as pool:
                healths = list(pool.map(reach.health, devices))
            for d, h in zip(devices, healths):
                d['health'] = h.to_dict()
        return devices

    # ── pinning ──
    def resolve_release(self, fleet: dict, ref) -> dict:
        if ref is None or ref == '':
            raise EnvError('/release', 'release is required (id or commit)')
        r = self.balena().find_release(fleet['id'], ref)
        if r is None:
            raise NotFound('release %s in %s' % (ref, fleet['slug']))
        if r.get('status') != 'success':
            raise EnvError('/release', 'release %s is %s, not success' % (ref, r.get('status')), status=409)
        if not r.get('is_final'):
            raise EnvError('/release', 'release %s is not final (finalize it first)' % ref, status=409)
        return r

    def pin_fleet(self, fleet: dict, release: dict, actor: str, change_id: Optional[str] = None) -> dict:
        previous = self.target_id(fleet)
        self.balena().pin_fleet(fleet['id'], release['id'])
        self.record_target(fleet['slug'], '', release['id'], actor, change_id)
        return {'fleet': fleet['slug'], 'previous_release_id': previous, 'target_release_id': release['id'],
                'target_release': release['commit']}

    def pin_device(self, device: dict, release: Optional[dict], actor: str, change_id: Optional[str] = None) -> dict:
        fleet = self.fleet_of(device)
        previous = ref_id(device.get('should_be_running__release'))
        self.balena().pin_device(device['id'], release['id'] if release else None)
        self.record_target(fleet['slug'], device['uuid'], release['id'] if release else None, actor, change_id)
        return {'uuid': device['uuid'], 'fleet': fleet['slug'], 'previous_pinned_release_id': previous,
                'pinned_release_id': release['id'] if release else None,
                'target_release': release['commit'] if release else None}

    def record_target(self, fleet: str, device: str, release_id: Optional[int], actor: str,
                      change_id: Optional[str] = None):
        db = self.session_factory()
        try:
            row = db.get(AppliedTarget, (fleet, device or ''))
            if row is None:
                row = AppliedTarget(fleet=fleet, device=device or '')
                db.add(row)
            row.release_id, row.applied_by, row.change_id, row.applied_at = release_id, actor, change_id, utcnow()
            db.commit()
        finally:
            db.close()

    # ── env ──
    def fleet_env(self, fleet: dict) -> List[dict]:
        return EnvStore(self.balena()).fleet_rows(fleet, self.patterns)

    def env_snapshot(self, fleet: dict) -> str:
        """sha256 of the fleet's fleet- and service-level vars (for change-set base state)."""
        rows = EnvStore(self.balena()).fleet_rows(fleet, ())
        return value_sha256('\n'.join('%s\t%s\t%s' % (r['service'], r['name'], r['value']) for r in rows))

    def patch_env(self, fleet: dict, device: Optional[dict], service: str, set_vars: Mapping[str, str],
                  unset, actor: str, change_id: Optional[str] = None) -> dict:
        store = EnvStore(self.balena())
        previous = store.apply(fleet, device, service, set_vars, unset)
        self._record_env(fleet['slug'], device['uuid'] if device else '', service, set_vars, unset, actor, change_id)
        p = self.patterns
        return {'fleet': fleet['slug'], 'device': device['uuid'] if device else None, 'service': service or None,
                'set': sorted(set_vars), 'unset': sorted(unset),
                'changed': [{'name': n, 'from': masked(n, previous.get(n), p),
                             'to': masked(n, set_vars.get(n), p)} for n in sorted(previous)
                            if previous.get(n) != set_vars.get(n)],
                '_previous': previous}

    def restore_env(self, fleet: dict, device: Optional[dict], service: str, previous: Mapping, actor: str,
                    change_id: Optional[str] = None):
        set_vars = {k: v for k, v in previous.items() if v is not None}
        unset = [k for k, v in previous.items() if v is None]
        EnvStore(self.balena()).apply(fleet, device, service, set_vars, unset)
        self._record_env(fleet['slug'], device['uuid'] if device else '', service, set_vars, unset, actor, change_id)

    def env_diff(self, fleet: dict, device: Optional[dict], service: str, set_vars, unset) -> List[dict]:
        current = EnvStore(self.balena()).read(fleet, device, service)
        p = self.patterns
        out = []
        for n, v in sorted(set_vars.items()):
            old = current[n]['value'] if n in current else None
            if old != v:
                out.append({'name': n, 'service': service or None, 'from': masked(n, old, p), 'to': masked(n, v, p)})
        for n in sorted(unset):
            if n in current:
                out.append({'name': n, 'service': service or None, 'from': masked(n, current[n]['value'], p),
                            'to': None})
        return out

    def _record_env(self, fleet: str, device: str, service: str, set_vars, unset, actor, change_id):
        p = self.patterns
        db = self.session_factory()
        try:
            for name, value, present in [(n, v, True) for n, v in set_vars.items()] + [(n, None, False) for n in unset]:
                row = db.execute(select(AppliedEnv).where(AppliedEnv.fleet == fleet, AppliedEnv.device == device,
                                                          AppliedEnv.service == service, AppliedEnv.name == name)
                                 ).scalar_one_or_none()
                if row is None:
                    row = AppliedEnv(fleet=fleet, device=device, service=service, name=name)
                    db.add(row)
                row.present = present
                row.value = None if (value is None or is_secret(name, p)) else value
                row.value_sha256 = value_sha256(value)
                row.applied_by, row.change_id, row.applied_at = actor, change_id, utcnow()
            db.commit()
        finally:
            db.close()

    # ── device operations ──
    def device_operation(self, device: dict, op: str, params: Mapping) -> dict:
        b = self.balena()
        if op == 'restart_service':
            service = params.get('service')
            if not isinstance(service, str) or not service:
                raise EnvError('/service', 'restart_service needs {"service": "<name>"}')
            app_id = ref_id(device.get('belongs_to__application'))
            b.restart_service(device['id'], app_id, service)
            return {'uuid': device['uuid'], 'type': op, 'service': service, 'state': 'requested'}
        if op == 'reboot':
            b.reboot(device['id'], force=bool(params.get('force')))
            return {'uuid': device['uuid'], 'type': op, 'force': bool(params.get('force')), 'state': 'requested'}
        raise EnvError('/type', 'type must be restart_service or reboot')

    # ── drift ──
    def drift(self, fleet_ref: Optional[str] = None) -> List[dict]:
        b = self.balena()
        fleets = [self.fleet(fleet_ref)] if fleet_ref else b.fleets()
        out = []
        db = self.session_factory()
        try:
            applied_env = db.execute(select(AppliedEnv)).scalars().all()
            applied_targets = db.execute(select(AppliedTarget)).scalars().all()
        finally:
            db.close()
        p = self.patterns
        for f in fleets:
            slug, target = f['slug'], self.target_id(f)
            commits = self._commits(f['id'])
            devices = [normalize_device(d, commits, target) for d in b.devices(f['id'])]
            by_uuid = {d['uuid']: d for d in devices}
            for d in devices:
                if d['target_release_id'] and d['release_id'] != d['target_release_id']:
                    out.append({'kind': 'release', 'fleet': slug, 'device': d['uuid'], 'name': d['name'],
                                'online': d['online'],
                                'expected': d['target_release'] or d['target_release_id'],
                                'actual': d['release'] or d['release_id']})
            for t in (t for t in applied_targets if t.fleet == slug):
                if not t.device:
                    if t.release_id != target:
                        out.append({'kind': 'target_release', 'fleet': slug, 'device': None,
                                    'expected': commits.get(t.release_id, t.release_id),
                                    'actual': commits.get(target, target), 'detail': 'fleet target changed outside the API'})
                    continue
                d = by_uuid.get(t.device)
                if d is not None and d['pinned_release_id'] != t.release_id:
                    out.append({'kind': 'target_release', 'fleet': slug, 'device': t.device, 'name': d['name'],
                                'expected': commits.get(t.release_id, t.release_id),
                                'actual': commits.get(d['pinned_release_id'], d['pinned_release_id']),
                                'detail': 'device pin changed outside the API'})
            out.extend(self._env_drift(f, [e for e in applied_env if e.fleet == slug], by_uuid, p))
        return out

    def _env_drift(self, fleet: dict, rows, devices_by_uuid, patterns) -> List[dict]:
        out = []
        store = EnvStore(self.balena())
        scopes = {}
        for r in rows:
            scopes.setdefault((r.device, r.service), []).append(r)
        for (uuid, service), entries in sorted(scopes.items()):
            device = None
            if uuid:
                device = self.balena().device(uuid)
                if device is None:
                    continue
            try:
                live = store.read(fleet, device, service)
            except (EnvError, BalenaError) as e:
                out.append({'kind': 'env', 'fleet': fleet['slug'], 'device': uuid or None, 'service': service or None,
                            'name': None, 'detail': 'cannot read: %s' % getattr(e, 'message', e)})
                continue
            for e in entries:
                cur = live.get(e.name)
                cur_val = cur.get('value') if cur else None
                if bool(cur) == e.present and (not e.present or value_sha256(cur_val) == e.value_sha256):
                    continue
                expected = None if not e.present else (e.value if e.value is not None else '***')
                out.append({'kind': 'env', 'fleet': fleet['slug'], 'device': uuid or None, 'service': service or None,
                            'name': e.name, 'expected': expected, 'actual': masked(e.name, cur_val, patterns),
                            'detail': 'changed outside the API' if cur and e.present else
                            ('removed outside the API' if e.present else 'set outside the API after the API removed it')})
        return out
