"""Change set signatures, verified exactly as metrolla-cli documents ("Change set signatures").

  payload   = canonical_json(request body with the top-level "signature" key removed)
  header    = canonical_json({"alg": "EdDSA", "kid": <kid>})
  input     = b64url(header) + "." + b64url(payload)
  jws       = input + "." + b64url(Ed25519(input))

The body carries {"signature": {"kid", "jws"}}. The server requires alg EdDSA, the header kid
equal to signature.kid, a trusted key for that kid, the JWS payload equal byte for byte to the
canonical body it received, and a valid Ed25519 signature over input.
"""
import base64
import json
from typing import Mapping, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .canonical import b64url, b64url_decode, canonical_json


class SignatureError(ValueError):
    """code: signature-required | signature-malformed | unknown-key | signature-mismatch |
    signature-invalid."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code, self.detail = code, detail


def signing_payload(body: Mapping) -> bytes:
    return canonical_json({k: v for k, v in body.items() if k != 'signature'})


def public_key_b64(key) -> str:
    """Base64 DER SubjectPublicKeyInfo ("MCowBQYDK2VwAyEA…"), the trusted_keys[].ed25519 form."""
    pub = key.public_key() if isinstance(key, Ed25519PrivateKey) else key
    der = pub.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return base64.b64encode(der).decode('ascii')


def load_public_key_b64(text: str) -> Ed25519PublicKey:
    key = serialization.load_der_public_key(base64.b64decode(text))
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError('not an Ed25519 public key')
    return key


def sign_change(body: Mapping, kid: str, key: Ed25519PrivateKey) -> dict:
    header = b64url(canonical_json({'alg': 'EdDSA', 'kid': kid}))
    payload = b64url(signing_payload(body))
    sig = key.sign(('%s.%s' % (header, payload)).encode('ascii'))
    return {'kid': kid, 'jws': '%s.%s.%s' % (header, payload, b64url(sig))}


def verify_change(body: Mapping, trusted_keys: Mapping[str, str]) -> str:
    """The kid that signed `body`, or SignatureError. trusted_keys maps kid -> base64 DER key."""
    sig = body.get('signature')
    if not isinstance(sig, Mapping):
        raise SignatureError('signature-malformed', 'signature must be an object {kid, jws}')
    kid, jws = sig.get('kid'), sig.get('jws')
    parts = jws.split('.') if isinstance(jws, str) else []
    if len(parts) != 3 or not isinstance(kid, str):
        raise SignatureError('signature-malformed', 'signature.jws is not a compact JWS')
    try:
        header = json.loads(b64url_decode(parts[0]))
        payload = b64url_decode(parts[1])
        raw_sig = b64url_decode(parts[2])
    except (ValueError, TypeError):
        raise SignatureError('signature-malformed', 'signature.jws does not decode')
    if not isinstance(header, dict) or header.get('alg') != 'EdDSA':
        raise SignatureError('signature-malformed', 'JWS alg must be EdDSA')
    if header.get('kid') != kid:
        raise SignatureError('signature-malformed', 'JWS kid does not match signature.kid')
    if kid not in trusted_keys:
        raise SignatureError('unknown-key', 'kid %r is not in change_control.trusted_keys' % kid)
    if payload != signing_payload(body):
        raise SignatureError('signature-mismatch', 'JWS payload does not match the change body')
    try:
        key = load_public_key_b64(trusted_keys[kid])
    except (ValueError, TypeError):
        raise SignatureError('unknown-key', 'trusted key %r is not a valid Ed25519 key' % kid)
    try:
        key.verify(raw_sig, ('%s.%s' % (parts[0], parts[1])).encode('ascii'))
    except InvalidSignature:
        raise SignatureError('signature-invalid', 'JWS signature does not verify')
    return kid


def verify_request(body: Mapping, trusted_keys: Mapping[str, str], require: bool) -> Optional[str]:
    """kid when the body is signed (and verifies); None when unsigned and not required."""
    if body.get('signature') is None:
        if require:
            raise SignatureError('signature-required', 'change_control.require_signature is on: sign the change set')
        return None
    return verify_change(body, trusted_keys)
