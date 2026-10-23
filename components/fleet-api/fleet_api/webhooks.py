"""Fleet change lifecycle webhooks.

webhooks:
  - url: https://servicenow.example/api/fleet-hook
    events: ["fleet.change.*"]          # fnmatch globs; default every event
    secret_file: /run/secrets/hook      # optional: X-Metrolla-Signature: sha256=<hmac of the body>

Every event carries the change id, state, fleets and the ticket reference, so a ticketing
system can follow the change. Delivery is best effort with three attempts.
"""
import fnmatch
import hashlib
import hmac
import json
import logging
import time
from typing import Callable, List

import requests

from .settings import read_secret

log = logging.getLogger('fleet_api')


class Notifier:
    def __init__(self, hooks: Callable[[], List[dict]], jobs, clock, session=None, sleep=time.sleep):
        self.hooks = hooks
        self.jobs = jobs
        self.clock = clock
        self.http = session or requests.Session()
        self.sleep = sleep

    def emit(self, event: str, payload: dict):
        body = dict(payload, type=event, ts=self.clock().isoformat())
        for hook in self.hooks() or []:
            if not isinstance(hook, dict) or not hook.get('url'):
                continue
            if not any(fnmatch.fnmatch(event, g) for g in (hook.get('events') or ['*'])):
                continue
            self.jobs.submit(self._deliver, hook, body)

    def _deliver(self, hook: dict, body: dict):
        data = json.dumps(body, sort_keys=True).encode()
        headers = {'Content-Type': 'application/json', 'X-Metrolla-Event': body['type']}
        secret = read_secret(hook.get('secret_file') or '')
        if secret:
            headers['X-Metrolla-Signature'] = 'sha256=' + hmac.new(secret.encode(), data, hashlib.sha256).hexdigest()
        for attempt in range(3):
            try:
                r = self.http.post(hook['url'], data=data, headers=headers, timeout=10)
                if r.status_code < 300:
                    return True
                log.warning('webhook %s answered %d for %s', hook['url'], r.status_code, body['type'])
            except requests.RequestException as e:
                log.warning('webhook %s failed for %s: %s', hook['url'], body['type'], type(e).__name__)
            if attempt < 2:
                self.sleep(2 ** attempt)
        return False
