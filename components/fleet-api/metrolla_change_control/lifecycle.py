"""Change-set lifecycle (F10): states, allowed transitions, approval rules, caller rules, and
the apply / wait / expire decision a scheduler makes for an approved change.

  draft -> planned -> approved -> scheduled -> applying -> verifying -> applied | rolled_back
  planned -> stale (base moved) -> planned (re-plan)
  draft | planned | approved | scheduled -> cancelled;  planned | approved | scheduled -> rejected
  scheduled -> expired (its window passed);  applying -> failed (nothing was changed)
  applied -> rolled_back (rollback requested)

Hosts keep the rows; these functions only decide.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional, Tuple

from .config import ChangeControlConfig, Policy
from .windows import Schedule

DRAFT, PLANNED, STALE, APPROVED, SCHEDULED = 'draft', 'planned', 'stale', 'approved', 'scheduled'
APPLYING, VERIFYING, APPLIED, ROLLED_BACK = 'applying', 'verifying', 'applied', 'rolled_back'
CANCELLED, EXPIRED, REJECTED, FAILED = 'cancelled', 'expired', 'rejected', 'failed'

STATES = (DRAFT, PLANNED, STALE, APPROVED, SCHEDULED, APPLYING, VERIFYING, APPLIED, ROLLED_BACK,
          CANCELLED, EXPIRED, REJECTED, FAILED)

TRANSITIONS = {
    DRAFT: {PLANNED, CANCELLED},
    PLANNED: {STALE, APPROVED, CANCELLED, REJECTED},
    STALE: {PLANNED, CANCELLED},
    APPROVED: {SCHEDULED, APPLYING, STALE, CANCELLED, REJECTED},
    SCHEDULED: {APPLYING, EXPIRED, STALE, CANCELLED, REJECTED},
    APPLYING: {VERIFYING, ROLLED_BACK, FAILED},
    VERIFYING: {APPLIED, ROLLED_BACK},
    APPLIED: {ROLLED_BACK},
}

TERMINAL = frozenset({APPLIED, ROLLED_BACK, CANCELLED, EXPIRED, REJECTED, FAILED})
# States a scheduler drives forward.
ACTIVE = frozenset({APPROVED, SCHEDULED, APPLYING, VERIFYING})


class TransitionError(ValueError):
    def __init__(self, frm: str, to: str):
        super().__init__('a %s change set cannot become %s' % (frm, to))
        self.frm, self.to = frm, to


class ApprovalError(ValueError):
    """code: not-approvable | plan-hash-mismatch | author-cannot-approve | already-approved."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code, self.detail = code, detail


class CallerError(ValueError):
    def __init__(self, kind: str, allowed: Tuple[str, ...]):
        super().__init__('%s callers may not author or apply change sets here (allowed: %s)'
                         % (kind, ', '.join(allowed)))
        self.kind, self.allowed = kind, allowed


def check_transition(frm: str, to: str) -> None:
    if to not in TRANSITIONS.get(frm, ()):
        raise TransitionError(frm, to)


def check_caller(cfg: ChangeControlConfig, kind: str) -> None:
    if cfg.allowed_callers and kind not in cfg.allowed_callers:
        raise CallerError(kind, cfg.allowed_callers)


def required_approvals(cfg: ChangeControlConfig, policy: Policy, emergency: bool) -> int:
    return max(policy.approvals, cfg.emergency_approvals) if emergency else policy.approvals


def check_approval(state: str, author: str, approver: str, current_hash: str, offered_hash: Optional[str],
                   approved_by: Iterable[str]) -> None:
    """Separation of duties and hash binding. Raises ApprovalError."""
    if state != PLANNED:
        raise ApprovalError('not-approvable', 'only a planned change set can be approved (this one is %s)' % state)
    if not offered_hash or offered_hash != current_hash:
        raise ApprovalError('plan-hash-mismatch',
                            'approve the current plan: send its plan_hash (%s)' % current_hash)
    if approver == author:
        raise ApprovalError('author-cannot-approve', 'the author of a change set cannot approve it')
    if approver in set(approved_by):
        raise ApprovalError('already-approved', 'you already approved this plan')


def approvals_met(approvals: Iterable[Tuple[str, str]], current_hash: str, required: int) -> bool:
    """approvals: (approver, plan_hash) pairs. Only approvals of the current hash count."""
    return len({a for a, h in approvals if h == current_hash}) >= required


def direct_write_allowed(cfg: ChangeControlConfig, resource_class: str) -> bool:
    """In gated mode a direct write is allowed only where the policy needs no approval and no
    window; everything else must arrive as a change set."""
    if not cfg.gated:
        return True
    p = cfg.policy_for(resource_class)
    return p.approvals == 0 and not p.window


@dataclass(frozen=True)
class Decision:
    action: str                                 # apply | wait | expire
    scheduled_for: Optional[datetime] = None
    window_end: Optional[datetime] = None
    reason: str = ''


def decide(cfg: ChangeControlConfig, policy: Policy, now: datetime, emergency: bool = False,
           not_before: Optional[datetime] = None, scheduled_window_end: Optional[datetime] = None,
           schedule: Optional[Schedule] = None) -> Decision:
    """What to do with an approved (or scheduled) change at `now`.

    Emergency changes apply at once and ignore windows and freezes. A change whose policy
    needs no window applies at once. Otherwise it applies inside an open, unfrozen window;
    if the window it was scheduled for has ended without it starting, it expires."""
    if emergency:
        return Decision('apply', reason='emergency')
    if not_before and now < not_before:
        return Decision('wait', scheduled_for=not_before, reason='not before %s' % not_before.isoformat())
    if not policy.window:
        return Decision('apply', reason='no window needed')
    if scheduled_window_end is not None and now >= scheduled_window_end:
        return Decision('expire', reason='its window ended at %s' % scheduled_window_end.isoformat())
    schedule = schedule or Schedule(cfg)
    open_now = schedule.window_at(now)
    if open_now:
        return Decision('apply', scheduled_for=open_now[0], window_end=open_now[1], reason='window open')
    nxt = schedule.next_open(now)
    if nxt is None:
        return Decision('wait', reason='no open window in the next 400 days')
    return Decision('wait', scheduled_for=nxt[0], window_end=nxt[1], reason='waiting for window')
