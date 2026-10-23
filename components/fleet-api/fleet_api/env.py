"""Env vars at fleet, fleet-service, device and device-service level: read (secrets masked),
patch, and the record of values applied through this API that drift is measured against.

A variable is a secret when its name contains one of env.secret_patterns (case-insensitive).
Secret values are never returned, and the applied-env record keeps only their sha256.
"""
import hashlib
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

from .balena import BalenaClient, ref_id

MASK = '***'

# scope key: (fleet slug, device uuid or '', service name or '')
Scope = Tuple[str, str, str]


def value_sha256(value: Optional[str]) -> Optional[str]:
    return None if value is None else hashlib.sha256(value.encode('utf-8')).hexdigest()


def is_secret(name: str, patterns: Iterable[str]) -> bool:
    upper = name.upper()
    return any(p.upper() in upper for p in patterns)


def masked(name: str, value: Optional[str], patterns: Iterable[str]) -> Optional[str]:
    if value is None:
        return None
    return MASK if is_secret(name, patterns) else value


class EnvError(ValueError):
    def __init__(self, pointer: str, message: str, status: int = 422):
        super().__init__(message)
        self.pointer, self.message, self.status = pointer, message, status


def validate_patch(body: Mapping) -> Tuple[Dict[str, str], List[str], str]:
    if not isinstance(body, Mapping):
        raise EnvError('', 'must be a JSON object {set, unset, service}')
    set_vars = body.get('set') or {}
    unset = body.get('unset') or []
    service = body.get('service') or ''
    if not isinstance(set_vars, Mapping) or not all(isinstance(k, str) and isinstance(v, (str, int, float, bool))
                                                    for k, v in set_vars.items()):
        raise EnvError('/set', 'must map names to string values')
    if not isinstance(unset, list) or not all(isinstance(n, str) for n in unset):
        raise EnvError('/unset', 'must be a list of names')
    if not isinstance(service, str):
        raise EnvError('/service', 'must be a service name')
    for name in list(set_vars) + unset:
        if not name or len(name) > 255 or not (name[0].isalpha() or name[0] == '_') \
                or not all(c.isalnum() or c == '_' for c in name):
            raise EnvError('/set' if name in set_vars else '/unset', 'invalid variable name %r' % name)
    both = set(set_vars) & set(unset)
    if both:
        raise EnvError('/unset', 'names both set and unset: %s' % ', '.join(sorted(both)))
    if not set_vars and not unset:
        raise EnvError('', 'nothing to change: give set and/or unset')
    return {k: str(v).lower() if isinstance(v, bool) else str(v) for k, v in set_vars.items()}, list(unset), service


class EnvStore:
    """Reads and writes one scope's variables through the openBalena API."""

    def __init__(self, balena: BalenaClient):
        self.balena = balena

    def _services(self, app_id: int) -> Dict[str, int]:
        return {s['service_name']: s['id'] for s in self.balena.services(app_id)}

    def _resolve(self, fleet: dict, device: Optional[dict], service: str):
        """(resource, parent field, parent id) for a scope."""
        if device is None and not service:
            return 'application_environment_variable', 'application', fleet['id']
        services = self._services(fleet['id'])
        if service and service not in services:
            raise EnvError('/service', 'fleet %s has no service %r' % (fleet['slug'], service), status=404)
        if device is None:
            return 'service_environment_variable', 'service', services[service]
        if not service:
            return 'device_environment_variable', 'device', device['id']
        installs = [i for i in self.balena.service_installs(device['id'])
                    if ref_id(i.get('installs__service')) == services[service]]
        if not installs:
            raise EnvError('/service', 'service %r is not installed on this device' % service, status=404)
        return 'device_service_environment_variable', 'service_install', installs[0]['id']

    def read(self, fleet: dict, device: Optional[dict] = None, service: str = '') -> Dict[str, dict]:
        """name -> row for one scope."""
        resource, parent, pid = self._resolve(fleet, device, service)
        rows = self.balena.get(resource, filter='%s eq %d' % (parent, pid), select='id,name,value')
        return {r['name']: r for r in rows}

    def fleet_rows(self, fleet: dict, patterns) -> List[dict]:
        """Every fleet-level and fleet-service variable, masked."""
        out = [{'name': r['name'], 'value': masked(r['name'], r.get('value'), patterns), 'service': '',
                'level': 'fleet', 'secret': is_secret(r['name'], patterns)}
               for r in self.balena.fleet_env(fleet['id'])]
        services = self._services(fleet['id'])
        by_id = {v: k for k, v in services.items()}
        for r in self.balena.service_env(services.values()):
            out.append({'name': r['name'], 'value': masked(r['name'], r.get('value'), patterns),
                        'service': by_id.get(ref_id(r.get('service')), ''), 'level': 'service',
                        'secret': is_secret(r['name'], patterns)})
        return sorted(out, key=lambda r: (r['service'], r['name']))

    def apply(self, fleet: dict, device: Optional[dict], service: str, set_vars: Mapping[str, str],
              unset: Iterable[str]) -> Dict[str, Optional[str]]:
        """Apply a patch; returns the previous value of every touched name (None = absent)."""
        resource, parent, pid = self._resolve(fleet, device, service)
        current = {r['name']: r for r in self.balena.get(resource, filter='%s eq %d' % (parent, pid),
                                                         select='id,name,value')}
        previous = {}
        for name, value in set_vars.items():
            row = current.get(name)
            previous[name] = row.get('value') if row else None
            if row is None or row.get('value') != value:
                self.balena.set_var(resource, parent, pid, name, value, row)
        for name in unset:
            row = current.get(name)
            previous[name] = row.get('value') if row else None
            if row is not None:
                self.balena.delete(resource, row['id'])
        return previous

    def restore(self, fleet: dict, device: Optional[dict], service: str, previous: Mapping[str, Optional[str]]):
        set_vars = {k: v for k, v in previous.items() if v is not None}
        unset = [k for k, v in previous.items() if v is None]
        self.apply(fleet, device, service, set_vars, unset)
