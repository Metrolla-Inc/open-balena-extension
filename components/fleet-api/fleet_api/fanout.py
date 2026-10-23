"""Site change fan-out: send one site change set to a fleet's devices in waves, through the
same DeviceReach, as POST /api/v1/changes on each device's EdgeAPI.

Each device applies (or schedules) the change under its own change control. A wave is done
when every device in it reports `applied`; then it soaks and the next wave is sent. A device
that ends rolled_back, failed, expired, cancelled or rejected, refuses the change, or can't be
reached while online halts the fan-out: later waves are not sent. Devices that are offline
when their wave is sent are skipped. Nothing is undone on devices that applied; each site
rolls back on its own checks.
"""
import hashlib
from datetime import datetime, timedelta
from typing import Callable, Dict, List

from .balena import BalenaClient, normalize_device
from .models import iso
from .reach import ReachError
from .rollout import plan_waves

SITE_OK = 'applied'
SITE_BAD = ('rolled_back', 'failed', 'expired', 'cancelled', 'rejected')


def idempotency_key(change_id: str, uuid: str) -> str:
    return 'fsc-' + hashlib.sha256(('%s\n%s' % (change_id, uuid)).encode()).hexdigest()[:32]


class FanoutEngine:
    def __init__(self, balena: Callable[[], BalenaClient], reach: Callable, timeout_min: Callable[[], int]):
        self.balena = balena
        self.reach = reach
        self.timeout_min = timeout_min

    def start(self, fleet: dict, change_id: str, body: dict, specs: List[dict], soak_min: int) -> dict:
        devices = [normalize_device(d) for d in self.balena().devices(fleet['id'])]
        waves = plan_waves(specs, devices, lambda d: True)
        return {
            'status': 'running', 'fleet': fleet['slug'], 'fleet_id': fleet['id'], 'fleet_change_id': change_id,
            'soak_min': soak_min, 'halt_reason': None, 'current': 0,
            'waves': [{'index': i, 'devices': [d['uuid'] for d in w], 'state': 'pending', 'sent_at': None,
                       'soak_until': None} for i, w in enumerate(waves)],
            'devices': {d['uuid']: {'name': d['name'], 'state': 'pending', 'change_id': None, 'error': None}
                        for w in waves for d in w},
            'body': body,
        }

    def tick(self, st: dict, now: datetime) -> dict:
        if st['status'] != 'running':
            return st
        while st['current'] < len(st['waves']) and not st['waves'][st['current']]['devices']:
            st['current'] += 1
        if st['current'] >= len(st['waves']):
            st['status'] = 'done'
            return st
        wave = st['waves'][st['current']]
        reach = self.reach()
        live: Dict[str, dict] = {d['uuid']: d for d in
                                 (normalize_device(x) for x in self.balena().devices(st['fleet_id']))}

        if wave['state'] == 'pending':
            for uuid in wave['devices']:
                rec, d = st['devices'][uuid], live.get(uuid)
                if d is None or not d['online']:
                    rec['state'] = 'skipped'
                    rec['error'] = 'offline when its wave was sent'
                    continue
                try:
                    status, resp = reach.request(d, 'POST', '/api/v1/changes', body=st['body'],
                                                 headers={'Idempotency-Key': idempotency_key(
                                                     st['fleet_change_id'], uuid)})
                except ReachError as e:
                    rec['state'], rec['error'] = 'failed', str(e)
                    return self._halt(st, '%s unreachable: %s' % (rec['name'] or uuid[:7], e))
                if status not in (200, 201, 202) or not isinstance(resp, dict) or not resp.get('id'):
                    detail = (resp.get('title') or resp.get('detail')) if isinstance(resp, dict) else resp
                    rec['state'], rec['error'] = 'failed', 'HTTP %s: %s' % (status, detail)
                    return self._halt(st, '%s refused the change (HTTP %s)' % (rec['name'] or uuid[:7], status))
                rec['change_id'], rec['state'] = resp['id'], resp.get('state') or 'planned'
            wave['state'], wave['sent_at'] = 'sent', iso(now)
            return st

        if wave['state'] == 'sent':
            waiting = []
            for uuid in wave['devices']:
                rec = st['devices'][uuid]
                if rec['state'] in ('skipped', SITE_OK):
                    continue
                d = live.get(uuid)
                if d is None or not d['online']:
                    waiting.append(rec['name'] or uuid[:7])
                    continue
                try:
                    status, resp = reach.request(d, 'GET', '/api/v1/changes/%s' % rec['change_id'])
                except ReachError:
                    waiting.append(rec['name'] or uuid[:7])
                    continue
                if status == 200 and isinstance(resp, dict):
                    rec['state'] = resp.get('state') or rec['state']
                if rec['state'] in SITE_BAD:
                    return self._halt(st, 'change %s on %s ended %s' % (rec['change_id'], rec['name'] or uuid[:7],
                                                                        rec['state']))
                if rec['state'] != SITE_OK:
                    waiting.append(rec['name'] or uuid[:7])
            if not waiting:
                wave['state'] = 'soaking'
                wave['soak_until'] = iso(now + timedelta(minutes=st['soak_min']))
            else:
                limit = st.get('timeout_min') or self.timeout_min()
                if now - datetime.fromisoformat(wave['sent_at']) >= timedelta(minutes=limit):
                    return self._halt(st, 'not applied within %d min on: %s' % (limit, ', '.join(waiting)))
            return st

        if wave['state'] == 'soaking' and now >= datetime.fromisoformat(wave['soak_until']):
            wave['state'] = 'done'
            st['current'] += 1
            if st['current'] >= len(st['waves']):
                st['status'] = 'done'
        return st

    def _halt(self, st: dict, reason: str) -> dict:
        st['status'], st['halt_reason'] = 'halted', reason
        return st


def summary(st) -> dict:
    if not st:
        return {}
    counts = {'updated': 0, 'pending': 0, 'failed': 0, 'skipped': 0}
    for rec in st['devices'].values():
        if rec['state'] == SITE_OK:
            counts['updated'] += 1
        elif rec['state'] == 'skipped':
            counts['skipped'] += 1
        elif rec['state'] in SITE_BAD or rec['state'] == 'failed':
            counts['failed'] += 1
        else:
            counts['pending'] += 1
    return {'wave': min(st['current'] + 1, len(st['waves'])), 'waves_total': len(st['waves']), 'devices': counts}
