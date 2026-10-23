"""Canonical JSON and the plan hash.

canonical_json is byte-identical to metrolla-cli's (mtl.signing.canonical_json): the CLI
signs it and the server re-derives it, so the two must never drift.
"""
import base64
import hashlib
import json
from typing import Any, Mapping


def canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False).encode('utf-8')


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


def plan_hash(base: Any, items: Any) -> str:
    """The hash approvals bind to. The same base state and the same items always give the same
    hash; anything else (title, ticket, window, timing) does not enter it."""
    return 'sha256:' + hashlib.sha256(canonical_json({'base': base, 'items': items})).hexdigest()


def content_digest(body: Mapping) -> str:
    """hex sha256 of the canonical body without its signature (the CLI's idempotency content)."""
    return hashlib.sha256(canonical_json({k: v for k, v in body.items() if k != 'signature'})).hexdigest()
