"""Fleet change lifecycle webhooks.

Two places name hooks, and both are delivered:

  webhooks:                             # fleet-api.yaml, top level
    - url: https://servicenow.example/api/fleet-hook
      events: ["fleet.change.*"]        # fnmatch globs; default every event
      secret_file: /run/secrets/hook    # or secret_env: NAME
  change_control:
    webhooks:                           # the shared metrolla_auth.change_control key, as on EdgeAPI
      - {url: https://itsm.example/hook, secret_env: ITSM_HOOK_SECRET, events: ["fleet.change.*"]}

Every event carries the change id, state, fleets and the ticket reference, so a ticketing
system can follow the change. Same wire format as EdgeAPI's change webhooks: the body is
canonical JSON, with X-Metrolla-Event, X-Metrolla-Delivery (unique per delivery) and, when the
hook has a secret, X-Metrolla-Signature: sha256=<HMAC-SHA256 of the body>. Delivery is best
effort with three attempts.
"""
import fnmatch
import logging
import os
import time
import uuid
from typing import Callable, Iterable, Mapping, Optional, Tuple

import requests

from metrolla_auth.change_control import SIGNATURE_HEADER, Webhook, canonical_json, signature

from .settings import read_secret

log = logging.getLogger('fleet_api')


def hook_target(hook, environ: Optional[Mapping] = None) -> Optional[Tuple[str, Tuple[str, ...], str]]:
    """(url, event globs, secret) for a top-level hook dict or a change_control Webhook."""
    environ = os.environ if environ is None else environ
    if isinstance(hook, Webhook):
        secret = environ.get(hook.secret_env, '') if hook.secret_env else hook.secret
        return hook.url, tuple(hook.events), secret or ''
    if not isinstance(hook, Mapping) or not hook.get('url'):
        return None
    secret = read_secret(hook.get('secret_file') or '', hook.get('secret_env') or '', environ)
    return hook['url'], tuple(hook.get('events') or ()), secret


class Notifier:
    def __init__(self, hooks: Callable[[], Iterable], jobs, clock, session=None, sleep=time.sleep,
                 environ: Optional[Mapping] = None):
        self.hooks = hooks
        self.jobs = jobs
        self.clock = clock
        self.http = session or requests.Session()
        self.sleep = sleep
        self.environ = environ

    def emit(self, event: str, payload: dict):
        body = dict(payload, type=event, ts=self.clock().isoformat())
        for hook in self.hooks() or []:
            target = hook_target(hook, self.environ)
            if target is None:
                continue
            url, events, secret = target
            if events and not any(fnmatch.fnmatch(event, g) for g in events):
                continue
            self.jobs.submit(self._deliver, url, secret, body)

    def _deliver(self, url: str, secret: str, body: dict):
        data = canonical_json(body)
        headers = {'Content-Type': 'application/json', 'X-Metrolla-Event': body['type'],
                   'X-Metrolla-Delivery': uuid.uuid4().hex}
        if secret:
            headers[SIGNATURE_HEADER] = signature(secret, data)
        for attempt in range(3):
            try:
                r = self.http.post(url, data=data, headers=headers, timeout=10)
                if r.status_code < 300:
                    return True
                log.warning('webhook %s answered %d for %s', url, r.status_code, body['type'])
            except requests.RequestException as e:
                log.warning('webhook %s failed for %s: %s', url, body['type'], type(e).__name__)
            if attempt < 2:
                self.sleep(2 ** attempt)
        return False
