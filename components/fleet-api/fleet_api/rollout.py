"""Wave rollouts of a release to a fleet, driven one tick at a time (no sleeping threads), so
the scheduler survives restarts and tests can drive it with a fake clock.

Waves: {"devices": [name or uuid, ...]} names canaries; {"percent": N} takes the fleet up to N%
of its eligible devices in total (cumulative, in name order). Devices pinned to some other
release before the rollout are left alone unless a wave names them.

Per wave: record each device's health and pin, pin it to the new release, wait until every online one runs it (within
rollout.update_timeout_min), then soak for soak_min while checking halt_if. Only when the
waves reach 100% does the fleet itself move to the new release (and the per-device pins are
dropped). halt_if {health: "worse"} halts when any rolled-out device's health ranks below
what it was just before it was pinned; {offline_devices: N} halts when N rolled-out devices
that were online have gone offline. Halting puts every touched device back on its previous
pin, so they follow the fleet's (unchanged) previous release again.
"""
import math
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional

from .balena import BalenaClient, normalize_device
from .models import iso
from .reach import worse

DEFAULT_HALT_IF = {'health': 'worse'}


class WaveError(ValueError):
    def __init__(self, pointer: str, message: str):
        super().__init__(message)
        self.pointer, self.message = pointer, message


def validate_waves(waves, pointer: str = '/waves') -> List[dict]:
    if waves is None:
        return [{'percent': 100}]
    if not isinstance(waves, list) or not waves:
        raise WaveError(pointer, 'must be a non-empty list of {devices: [...]} or {percent: N}')
    out = []
    last_pct = 0
    for i, w in enumerate(waves):
        p = '%s/%d' % (pointer, i)
        if not isinstance(w, dict) or len(w) != 1 or not ({'devices', 'percent'} & set(w)):
            raise WaveError(p, 'must be {devices: [...]} or {percent: N}')
        if 'devices' in w:
            devs = w['devices']
            if not isinstance(devs, list) or not devs or not all(isinstance(d, str) and d for d in devs):
                raise WaveError(p + '/devices', 'must list device names or uuids')
            out.append({'devices': list(devs)})
        else:
            pct = w['percent']
            if isinstance(pct, bool) or not isinstance(pct, int) or not 0 < pct <= 100 or pct < last_pct:
                raise WaveError(p + '/percent', 'must be 1-100 and not below an earlier wave')
            last_pct = pct
            out.append({'percent': pct})
    return out


def validate_halt_if(halt_if, pointer='/halt_if') -> dict:
    if halt_if is None:
        return dict(DEFAULT_HALT_IF)
    if not isinstance(halt_if, dict) or set(halt_if) - {'health', 'offline_devices'}:
        raise WaveError(pointer, 'must be {health: "worse", offline_devices: N}')
    if 'health' in halt_if and halt_if['health'] not in ('worse', None, 'off'):
        raise WaveError(pointer + '/health', 'only "worse" (or "off") is supported')
    n = halt_if.get('offline_devices')
    if n is not None and (isinstance(n, bool) or not isinstance(n, int) or n < 1):
        raise WaveError(pointer + '/offline_devices', 'must be an integer >= 1')
    return dict(halt_if)


def _match(ref: str, devices: List[dict]) -> Optional[dict]:
    ref_l = ref.lower()
    exact = [d for d in devices if d['uuid'] == ref_l or d.get('name') == ref]
    if len(exact) == 1:
        return exact[0]
    if len(ref_l) >= 7:
        pref = [d for d in devices if d['uuid'].startswith(ref_l)]
        if len(pref) == 1:
            return pref[0]
    return None


def plan_waves(specs: List[dict], devices: List[dict], eligible: Callable[[dict], bool],
               pointer='/waves') -> List[List[dict]]:
    """Device lists per wave. Raises WaveError for a name that matches no device."""
    pool = sorted([d for d in devices if eligible(d)], key=lambda d: ((d.get('name') or ''), d['uuid']))
    assigned: Dict[str, bool] = {}
    out = []
    for i, spec in enumerate(specs):
        wave = []
        if 'devices' in spec:
            for j, ref in enumerate(spec['devices']):
                d = _match(ref, devices)
                if d is None:
                    raise WaveError('%s/%d/devices/%d' % (pointer, i, j), 'no device %r in this fleet' % ref)
                if d['uuid'] not in assigned:
                    assigned[d['uuid']] = True
                    wave.append(d)
        else:
            target = math.ceil(len(pool) * spec['percent'] / 100.0)
            done = sum(1 for d in pool if d['uuid'] in assigned)
            for d in pool:
                if done >= target:
                    break
                if d['uuid'] not in assigned:
                    assigned[d['uuid']] = True
                    wave.append(d)
                    done += 1
        out.append(wave)
    return out


def reaches_fleet(specs: List[dict]) -> bool:
    return any(s.get('percent') == 100 for s in specs)


class RolloutEngine:
    def __init__(self, balena: Callable[[], BalenaClient], reach: Callable, update_timeout_min: Callable[[], int],
                 on_target: Callable[[str, str, Optional[int]], None] = lambda fleet, device, rid: None):
        self.balena = balena
        self.reach = reach
        self.update_timeout_min = update_timeout_min
        self.on_target = on_target

    # ── start ──
    def start(self, fleet: dict, release_id: int, specs: List[dict], soak_min: int, halt_if: dict,
              now: datetime) -> dict:
        b = self.balena()
        prev = fleet.get('should_be_running__release')
        prev_id = prev.get('__id') if isinstance(prev, dict) else (prev[0]['id'] if isinstance(prev, list) and prev
                                                                  else prev)
        devices = [normalize_device(d, fleet_target=prev_id) for d in b.devices(fleet['id'])]
        named = {d['uuid'] for s in specs if 'devices' in s for ref in s['devices'] for d in [_match(ref, devices)] if d}

        def eligible(d):
            return d['uuid'] in named or d['pinned_release_id'] in (None, prev_id, release_id)

        waves = plan_waves(specs, devices, eligible)
        return {
            'status': 'running', 'fleet': fleet['slug'], 'fleet_id': fleet['id'], 'release_id': release_id,
            'previous_release_id': prev_id, 'previous_tracks_latest': bool(fleet.get('should_track_latest_release')),
            'soak_min': soak_min, 'halt_if': halt_if, 'move_fleet': reaches_fleet(specs), 'fleet_pinned': False,
            'waves': [{'index': i, 'spec': specs[i], 'devices': [d['uuid'] for d in w], 'state': 'pending',
                       'started_at': None, 'soak_until': None} for i, w in enumerate(waves)],
            'current': 0,
            'devices': {d['uuid']: {'id': d['id'], 'name': d['name'], 'state': 'pending', 'health': None}
                        for w in waves for d in w},
            'baseline': {}, 'prior_pins': {}, 'halt_reason': None, 'started_at': iso(now),
        }

    # ── tick ──
    def tick(self, st: dict, now: datetime) -> dict:
        if st['status'] != 'running':
            return st
        b = self.balena()
        while st['current'] < len(st['waves']) and not st['waves'][st['current']]['devices']:
            st['waves'][st['current']]['state'] = 'done'
            st['current'] += 1
        if st['current'] >= len(st['waves']):
            return self._finish(st, b)
        wave = st['waves'][st['current']]
        live = {d['uuid']: d for d in (normalize_device(x) for x in b.devices(st['fleet_id']))}

        if wave['state'] == 'pending':
            # Record baselines and prior pins first (saved before anything is pinned), so a crash
            # between pinning and saving can never record the new release as the "prior" pin.
            reach = self.reach()
            for uuid in wave['devices']:
                d = live.get(uuid)
                if d is None:
                    st['devices'][uuid]['state'] = 'gone'
                    continue
                st['baseline'][uuid] = dict(reach.health(d).to_dict(), online=d['online'])
                if uuid not in st['prior_pins']:
                    st['prior_pins'][uuid] = d['pinned_release_id']
            wave['state'] = 'pinning'
            return st

        if wave['state'] == 'pinning':
            for uuid in wave['devices']:
                if uuid in st['prior_pins'] and st['devices'][uuid]['state'] != 'gone':
                    b.pin_device(st['devices'][uuid]['id'], st['release_id'])
                    st['devices'][uuid]['state'] = 'updating'
            wave['state'], wave['started_at'] = 'updating', iso(now)
            return st

        halted = self._check_halt(st, live, now)
        if halted:
            return halted

        if wave['state'] == 'updating':
            waiting = []
            for uuid in wave['devices']:
                d, rec = live.get(uuid), st['devices'][uuid]
                if d is None:
                    rec['state'] = 'gone'
                elif d['release_id'] == st['release_id']:
                    rec['state'] = 'updated'
                elif not d['online']:
                    rec['state'] = 'offline'
                else:
                    rec['state'] = 'updating'
                    waiting.append(rec['name'] or uuid[:7])
            updated = [u for u in wave['devices'] if st['devices'][u]['state'] == 'updated']
            started = datetime.fromisoformat(wave['started_at'])
            if not waiting and updated:
                wave['state'] = 'soaking'
                wave['soak_until'] = iso(now + timedelta(minutes=st['soak_min']))
            elif now - started >= timedelta(minutes=self.update_timeout_min()):
                reason = ('devices not on the release after %d min: %s' % (self.update_timeout_min(),
                                                                           ', '.join(waiting))
                          if waiting else 'no device in wave %d came online on the release' % (wave['index'] + 1))
                return self.halt(st, reason)
            return st

        if wave['state'] == 'soaking' and now >= datetime.fromisoformat(wave['soak_until']):
            wave['state'] = 'done'
            st['current'] += 1
            if st['current'] >= len(st['waves']):
                return self._finish(st, b)
        return st

    def _check_halt(self, st: dict, live: Dict[str, dict], now: datetime) -> Optional[dict]:
        halt_if = st.get('halt_if') or {}
        touched = [u for w in st['waves'] if w['state'] not in ('pending', 'pinning') for u in w['devices']]
        if halt_if.get('health') == 'worse':
            reach = self.reach()
            for uuid in touched:
                d = live.get(uuid)
                # Judge a device once it runs the release; mid-update restarts aren't a verdict.
                if d is None or not d['online'] or d['release_id'] != st['release_id']:
                    continue
                h = reach.health(d)
                st['devices'][uuid]['health'] = h.to_dict()
                if worse(h, st['baseline'].get(uuid)):
                    return self.halt(st, 'health got worse on %s: %s -> %s' % (
                        st['devices'][uuid]['name'] or uuid[:7], st['baseline'][uuid].get('status'), h.status))
        n = halt_if.get('offline_devices')
        if n:
            dropped = [st['devices'][u]['name'] or u[:7] for u in touched
                       if st['baseline'].get(u, {}).get('online') and (live.get(u) is None or not live[u]['online'])]
            if len(dropped) >= n:
                return self.halt(st, '%d device(s) went offline: %s' % (len(dropped), ', '.join(dropped)))
        return None

    def _finish(self, st: dict, b: BalenaClient) -> dict:
        if st['move_fleet'] and not st['fleet_pinned']:
            b.pin_fleet(st['fleet_id'], st['release_id'])
            st['fleet_pinned'] = True
            self.on_target(st['fleet'], '', st['release_id'])
            for uuid, prior in st['prior_pins'].items():
                b.pin_device(st['devices'][uuid]['id'], None)
                self.on_target(st['fleet'], uuid, None)
        elif not st['move_fleet']:
            for uuid in st['prior_pins']:
                self.on_target(st['fleet'], uuid, st['release_id'])
        st['status'] = 'done'
        return st

    # ── halt / revert ──
    def revert(self, st: dict):
        """Put every touched device back on its previous pin, and the fleet back on its previous
        release if the rollout had moved it."""
        b = self.balena()
        if st.get('fleet_pinned'):
            body = {'should_track_latest_release': st.get('previous_tracks_latest', False)}
            if st.get('previous_release_id') is not None:
                body['should_be_running__release'] = st['previous_release_id']
            b.patch('application', body, id=st['fleet_id'])
            st['fleet_pinned'] = False
            self.on_target(st['fleet'], '', st.get('previous_release_id'))
        for uuid, prior in st['prior_pins'].items():
            b.pin_device(st['devices'][uuid]['id'], prior)
            st['devices'][uuid]['state'] = 'reverted'
            self.on_target(st['fleet'], uuid, prior)

    def halt(self, st: dict, reason: str) -> dict:
        self.revert(st)
        st['status'], st['halt_reason'] = 'halted', reason
        return st


def summary(st: Optional[dict]) -> dict:
    """{wave, devices: {updated, pending, failed}} as GET /changes/{id} shows it."""
    if not st:
        return {}
    counts = {'updated': 0, 'pending': 0, 'failed': 0}
    for rec in st['devices'].values():
        s = rec['state']
        if s == 'updated':
            counts['updated'] += 1
        elif s in ('pending', 'updating', 'offline'):
            counts['pending'] += 1
        else:
            counts['failed'] += 1
    return {'wave': min(st['current'] + 1, len(st['waves'])), 'waves_total': len(st['waves']), 'devices': counts}
