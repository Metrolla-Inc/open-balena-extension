"""metrolla_change_control: the generic half of change control (F10), shared by the Fleet API
and, later, EdgeAPI (M2b).

Nothing here knows about openBalena, fleets or devices. It holds:
- canonical JSON and the deterministic plan hash approvals bind to (canonical)
- compact-JWS EdDSA signatures over the change body, as metrolla-cli signs them (signing)
- the `change_control` config section: mode, callers, keys, windows, freezes, policy (config)
- release windows (RRULE in the site time zone) and freezes (windows)
- the change-set state machine, approval rules and the apply/wait/expire decision (lifecycle)

It is written to move into metrolla_auth as metrolla_auth.change_control without changes
other than the import path.
"""
from .canonical import b64url, b64url_decode, canonical_json, content_digest, plan_hash
from .config import (ChangeControlConfig, ChangeControlConfigError, Freeze, Policy, Window,
                     parse_change_control)
from .lifecycle import (ACTIVE, APPLIED, APPLYING, APPROVED, CANCELLED, DRAFT, EXPIRED, FAILED, PLANNED,
                        REJECTED, ROLLED_BACK, SCHEDULED, STALE, STATES, TERMINAL, TRANSITIONS, VERIFYING,
                        ApprovalError, CallerError, Decision, TransitionError, approvals_met, check_approval,
                        check_caller, check_transition, decide, direct_write_allowed, required_approvals)
from .signing import SignatureError, public_key_b64, sign_change, signing_payload, verify_change, verify_request
from .windows import Schedule
