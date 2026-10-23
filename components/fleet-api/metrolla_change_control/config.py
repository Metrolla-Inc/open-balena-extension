"""The `change_control` config section (F10) and its validation.

change_control:
  mode: open | gated            # open = direct writes, as today
  allowed_callers: [machine]    # caller kinds that may author or apply; empty = any
  require_signature: false
  trusted_keys: [{kid, ed25519}]
  timezone: America/New_York    # windows are wall-clock in this zone
  windows: [{name, rrule, duration_min}]
  freezes: [{start, end, reason}]
  verify: {timeout_s}
  emergency: {approvals}
  policy: {<resource class>: {approvals, window}}

Resource classes are dotted names; a policy lookup falls back to the parent class
('op.device.reboot' -> 'op.device' -> 'op') and then to `default`.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Iterable, List, Mapping, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.rrule import rrulestr

from .signing import load_public_key_b64

MODES = ('open', 'gated')
CALLER_KINDS = ('interactive', 'machine', 'anonymous', 'emergency', 'plugin')


class ChangeControlConfigError(ValueError):
    def __init__(self, errors: List[Dict[str, str]]):
        self.errors = errors
        super().__init__('; '.join('%s %s' % (e['pointer'], e['message']) for e in errors))


@dataclass(frozen=True)
class Policy:
    approvals: int = 0
    window: bool = False

    def combine(self, other: 'Policy') -> 'Policy':
        return Policy(max(self.approvals, other.approvals), self.window or other.window)

    def to_dict(self):
        return {'approvals': self.approvals, 'window': self.window}


@dataclass(frozen=True)
class Window:
    name: str
    rrule: str
    duration_min: int


@dataclass(frozen=True)
class Freeze:
    start: datetime
    end: datetime
    reason: str = ''

    def covers(self, t: datetime) -> bool:
        return self.start <= t < self.end


@dataclass(frozen=True)
class ChangeControlConfig:
    mode: str = 'open'
    allowed_callers: Tuple[str, ...] = ()
    require_signature: bool = False
    trusted_keys: Mapping[str, str] = field(default_factory=dict)
    timezone: str = 'UTC'
    windows: Tuple[Window, ...] = ()
    freezes: Tuple[Freeze, ...] = ()
    verify_timeout_s: int = 120
    emergency_approvals: int = 2
    policy: Mapping[str, Policy] = field(default_factory=dict)
    default_policy: Policy = Policy()

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def gated(self) -> bool:
        return self.mode == 'gated'

    def policy_for(self, resource_class: str) -> Policy:
        name = resource_class
        while name:
            if name in self.policy:
                return self.policy[name]
            name = name.rpartition('.')[0]
        return self.default_policy

    def combined_policy(self, classes: Iterable[str]) -> Policy:
        out = Policy()
        for c in classes:
            out = out.combine(self.policy_for(c))
        return out

    def to_dict(self) -> dict:
        return {
            'mode': self.mode, 'allowed_callers': list(self.allowed_callers),
            'require_signature': self.require_signature,
            'trusted_keys': [{'kid': k} for k in sorted(self.trusted_keys)],
            'timezone': self.timezone,
            'windows': [{'name': w.name, 'rrule': w.rrule, 'duration_min': w.duration_min} for w in self.windows],
            'freezes': [{'start': f.start.isoformat(), 'end': f.end.isoformat(), 'reason': f.reason}
                        for f in self.freezes],
            'verify': {'timeout_s': self.verify_timeout_s},
            'emergency': {'approvals': self.emergency_approvals},
            'policy': {k: v.to_dict() for k, v in sorted(self.policy.items())},
            'default': self.default_policy.to_dict(),
        }


def _int(v, pointer, errors, minimum=0) -> Optional[int]:
    if isinstance(v, bool) or not isinstance(v, int) or v < minimum:
        errors.append({'pointer': pointer, 'message': 'must be an integer >= %d' % minimum})
        return None
    return v


def _bool(v, pointer, errors) -> bool:
    if not isinstance(v, bool):
        errors.append({'pointer': pointer, 'message': 'must be true or false'})
        return False
    return v


def _time(v, pointer, errors, tz) -> Optional[datetime]:
    if isinstance(v, datetime):
        dt = v
    else:
        try:
            dt = datetime.fromisoformat(str(v).replace('Z', '+00:00'))
        except ValueError:
            errors.append({'pointer': pointer, 'message': 'must be an RFC 3339 time'})
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=tz)


def _policy(raw, pointer, errors) -> Policy:
    if not isinstance(raw, Mapping):
        errors.append({'pointer': pointer, 'message': 'must be {approvals, window}'})
        return Policy()
    unknown = set(raw) - {'approvals', 'window'}
    for k in sorted(unknown):
        errors.append({'pointer': '%s/%s' % (pointer, k), 'message': 'unknown key'})
    approvals = _int(raw.get('approvals', 0), pointer + '/approvals', errors)
    window = _bool(raw.get('window', False), pointer + '/window', errors)
    return Policy(approvals or 0, window)


def parse_change_control(raw: Optional[Mapping]) -> ChangeControlConfig:
    """Validated config, or ChangeControlConfigError with JSON-Pointer errors."""
    raw = raw or {}
    errors: List[Dict[str, str]] = []
    if not isinstance(raw, Mapping):
        raise ChangeControlConfigError([{'pointer': '', 'message': 'must be a mapping'}])
    mode = raw.get('mode', 'open')
    if mode not in MODES:
        errors.append({'pointer': '/mode', 'message': 'must be one of %s' % ', '.join(MODES)})
    callers = raw.get('allowed_callers') or []
    if not isinstance(callers, list) or any(c not in CALLER_KINDS for c in callers):
        errors.append({'pointer': '/allowed_callers', 'message': 'must list caller kinds: %s' % ', '.join(CALLER_KINDS)})
        callers = []
    require_sig = _bool(raw.get('require_signature', False), '/require_signature', errors)

    keys: Dict[str, str] = {}
    for i, k in enumerate(raw.get('trusted_keys') or []):
        if not isinstance(k, Mapping) or not isinstance(k.get('kid'), str) or not isinstance(k.get('ed25519'), str):
            errors.append({'pointer': '/trusted_keys/%d' % i, 'message': 'must be {kid, ed25519}'})
            continue
        try:
            load_public_key_b64(k['ed25519'])
        except (ValueError, TypeError):
            errors.append({'pointer': '/trusted_keys/%d/ed25519' % i,
                           'message': 'must be a base64 DER Ed25519 public key'})
            continue
        keys[k['kid']] = k['ed25519']
    if require_sig and not keys:
        errors.append({'pointer': '/require_signature', 'message': 'needs at least one trusted key'})

    tz_name = raw.get('timezone', 'UTC')
    try:
        tz = ZoneInfo(str(tz_name))
    except (ZoneInfoNotFoundError, ValueError):
        errors.append({'pointer': '/timezone', 'message': 'unknown time zone'})
        tz, tz_name = ZoneInfo('UTC'), 'UTC'

    windows = []
    for i, w in enumerate(raw.get('windows') or []):
        p = '/windows/%d' % i
        if not isinstance(w, Mapping) or not isinstance(w.get('rrule'), str):
            errors.append({'pointer': p, 'message': 'must be {name, rrule, duration_min}'})
            continue
        try:
            rrulestr(w['rrule'], dtstart=datetime(2024, 1, 1, tzinfo=tz))
        except (ValueError, TypeError) as e:
            errors.append({'pointer': p + '/rrule', 'message': 'not a valid RRULE: %s' % e})
            continue
        dur = _int(w.get('duration_min'), p + '/duration_min', errors, minimum=1)
        if dur:
            windows.append(Window(str(w.get('name') or 'window-%d' % i), w['rrule'], dur))

    freezes = []
    for i, f in enumerate(raw.get('freezes') or []):
        p = '/freezes/%d' % i
        if not isinstance(f, Mapping):
            errors.append({'pointer': p, 'message': 'must be {start, end, reason}'})
            continue
        start = _time(f.get('start'), p + '/start', errors, tz)
        end = _time(f.get('end'), p + '/end', errors, tz)
        if start and end:
            if end <= start:
                errors.append({'pointer': p + '/end', 'message': 'must be after start'})
            else:
                freezes.append(Freeze(start, end, str(f.get('reason') or '')))

    verify = raw.get('verify') or {}
    timeout = _int(verify.get('timeout_s', 120), '/verify/timeout_s', errors, minimum=1) if isinstance(verify, Mapping) else 120
    emergency = raw.get('emergency') or {}
    em_approvals = _int(emergency.get('approvals', 2), '/emergency/approvals', errors) \
        if isinstance(emergency, Mapping) else 2

    policy = {}
    pol_raw = raw.get('policy') or {}
    if not isinstance(pol_raw, Mapping):
        errors.append({'pointer': '/policy', 'message': 'must be a mapping of resource class to policy'})
        pol_raw = {}
    default = Policy()
    for name, p in pol_raw.items():
        parsed = _policy(p, '/policy/%s' % name, errors)
        if name == 'default':
            default = parsed
        else:
            policy[str(name)] = parsed

    unknown = set(raw) - {'mode', 'allowed_callers', 'require_signature', 'trusted_keys', 'timezone', 'windows',
                          'freezes', 'verify', 'emergency', 'policy'}
    for k in sorted(unknown):
        errors.append({'pointer': '/' + str(k), 'message': 'unknown key'})
    if errors:
        raise ChangeControlConfigError(errors)
    return ChangeControlConfig(mode=mode, allowed_callers=tuple(callers), require_signature=require_sig,
                               trusted_keys=keys, timezone=str(tz_name), windows=tuple(windows),
                               freezes=tuple(sorted(freezes, key=lambda f: f.start)),
                               verify_timeout_s=timeout or 120,
                               emergency_approvals=em_approvals if em_approvals is not None else 2,
                               policy=policy, default_policy=default)
