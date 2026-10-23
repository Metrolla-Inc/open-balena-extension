"""Fleet change sets (F11 on top of F10): release.build, env.patch, release.deploy and
site.change items, planned against the fleets' current state, approved against the plan
hash, applied inside windows by the scheduler, verified, and rolled back on failure.

The generic rules (state machine, plan hash, approvals, windows, freezes, signatures) come
from metrolla_auth.change_control; this module only knows how to plan, apply, verify and undo
fleet items.

Base state (what makes a plan stale): per fleet, its target release when a deploy item rolls
it, the env snapshot of every scope an env.patch item touches, and the release a deploy item
names. If any of it
moves between planning and applying, the change becomes `stale` and must be re-planned (which
gives a new hash, so earlier approvals no longer count).
"""
import copy
import json
import logging
import threading
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple

from sqlalchemy import select

from metrolla_auth import change_control as cc
from metrolla_auth.change_control import Schedule, SignatureError, verify_request

from .balena import BalenaError, normalize_device
from .env import EnvError, EnvStore, is_secret, masked, validate_patch, value_sha256
from .fanout import FanoutEngine, summary as fanout_summary
from .gitsrc import GitError, validate as validate_git
from .models import ChangeApproval, ChangeEvent, ChangeSet, aware, iso, new_id
from .reach import worse
from .rollout import RolloutEngine, WaveError, plan_waves, summary as rollout_summary, validate_halt_if, validate_waves
from .service import FleetService, NotFound

log = logging.getLogger('fleet_api')

OPS = ('release.build', 'env.patch', 'release.deploy', 'site.change')
RESOURCE_CLASS = {'release.build': 'release.build', 'env.patch': 'env', 'release.deploy': 'release.deploy',
                  'site.change': 'site.change'}
BUILT = '@built'
_LOCK = threading.RLock()


class ChangeError(Exception):
    def __init__(self, status: int, code: str, title: str, detail: Optional[str] = None,
                 errors: Optional[List[dict]] = None):
        super().__init__(detail or title)
        self.status, self.code, self.title, self.detail, self.errors = status, code, title, detail, errors


def _invalid(pointer: str, message: str) -> ChangeError:
    return ChangeError(422, 'validation', 'Change set is not valid', errors=[{'pointer': pointer, 'message': message}])


def _jsonable(value):
    return json.loads(json.dumps(value, default=str))


class FleetChanges:
    def __init__(self, session_factory, fleet: FleetService, builds, rollout: RolloutEngine, fanout: FanoutEngine,
                 settings: Callable, clock: Callable[[], datetime], notifier, audit, git: Callable):
        self.session_factory = session_factory
        self.fleet = fleet
        self.builds = builds
        self.rollout = rollout
        self.fanout = fanout
        self.settings = settings
        self.clock = clock
        self.notifier = notifier
        self.audit = audit
        self.git = git

    @property
    def cfg(self) -> cc.ChangeControlConfig:
        return self.settings().change_control

    # ── validation and planning ──
    def _normalize(self, items) -> List[dict]:
        if not isinstance(items, list) or not items:
            raise _invalid('/items', 'must be a non-empty list')
        out, built = [], set()
        for i, it in enumerate(items):
            p = '/items/%d' % i
            if not isinstance(it, dict) or it.get('op') not in OPS:
                raise _invalid(p + '/op', 'must be one of %s' % ', '.join(OPS))
            fleet = it.get('fleet')
            if not isinstance(fleet, str) or not fleet:
                raise _invalid(p + '/fleet', 'is required')
            op = it['op']
            try:
                if op == 'release.build':
                    git = validate_git(it.get('git'))
                    self.git().resolve(git['repo'])
                    out.append({'op': op, 'fleet': fleet, 'git': git})
                    built.add(fleet)
                elif op == 'env.patch':
                    set_vars, unset, service = validate_patch(it)
                    item = {'op': op, 'fleet': fleet, 'set': set_vars, 'unset': unset, 'service': service}
                    if it.get('device'):
                        item['device'] = str(it['device']).lower()
                    out.append(item)
                elif op == 'release.deploy':
                    release = it.get('release')
                    if release == BUILT:
                        if fleet not in built:
                            raise _invalid(p + '/release', '"@built" needs an earlier release.build for %s' % fleet)
                    elif not isinstance(release, (int, str)) or isinstance(release, bool) or release == '':
                        raise _invalid(p + '/release', 'must be a release id, a commit or "@built"')
                    soak = it.get('soak_min', 0)
                    if isinstance(soak, bool) or not isinstance(soak, int) or soak < 0:
                        raise _invalid(p + '/soak_min', 'must be minutes >= 0')
                    out.append({'op': op, 'fleet': fleet, 'release': release,
                                'waves': validate_waves(it.get('waves'), p + '/waves'), 'soak_min': soak,
                                'halt_if': validate_halt_if(it.get('halt_if'), p + '/halt_if')})
                else:
                    body = it.get('change')
                    if not isinstance(body, dict) or not isinstance(body.get('items'), list):
                        raise _invalid(p + '/change', 'must be a site change set body with items')
                    soak = it.get('soak_min', 0)
                    timeout = it.get('timeout_min')
                    if isinstance(soak, bool) or not isinstance(soak, int) or soak < 0:
                        raise _invalid(p + '/soak_min', 'must be minutes >= 0')
                    if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 1):
                        raise _invalid(p + '/timeout_min', 'must be minutes >= 1')
                    item = {'op': op, 'fleet': fleet, 'change': body,
                            'waves': validate_waves(it.get('waves'), p + '/waves'), 'soak_min': soak}
                    if timeout is not None:
                        item['timeout_min'] = timeout
                    out.append(item)
            except EnvError as e:
                raise _invalid(p + e.pointer, e.message)
            except (WaveError, GitError) as e:
                ptr = e.pointer if e.pointer.startswith('/items') else p + e.pointer
                raise _invalid(ptr, e.message)
        return out

    def _resolve(self, items: List[dict]) -> Dict[str, dict]:
        fleets = {}
        for i, it in enumerate(items):
            if it['fleet'] not in fleets:
                try:
                    fleets[it['fleet']] = self.fleet.fleet(it['fleet'])
                except NotFound:
                    raise _invalid('/items/%d/fleet' % i, 'no fleet %r' % it['fleet'])
        return fleets

    def _device(self, uuid: str, fleet: dict, pointer: str) -> dict:
        d = self.fleet.balena().device(uuid)
        if d is None or self.fleet.fleet_of(d)['id'] != fleet['id']:
            raise _invalid(pointer, 'no device %s in %s' % (uuid, fleet['slug']))
        return d

    def _base(self, items: List[dict], fleets: Dict[str, dict]) -> dict:
        base = {}
        for i, it in enumerate(items):
            f = fleets[it['fleet']]
            entry = base.setdefault(f['slug'], {})
            if it['op'] == 'release.deploy':
                entry['target_release'] = self.fleet.target_id(f)
            if it['op'] == 'env.patch':
                if it.get('device'):
                    d = self._device(it['device'], f, '/items/%d/device' % i)
                    rows = self._scope_rows(f, d, it['service'])
                    entry.setdefault('device_env', {})['%s/%s' % (d['uuid'], it['service'])] = rows
                else:
                    entry['env'] = self.fleet.env_snapshot(f)
            elif it['op'] == 'release.deploy' and it['release'] != BUILT:
                try:
                    r = self.fleet.resolve_release(f, it['release'])
                except NotFound as e:
                    raise _invalid('/items/%d/release' % i, 'no %s' % e.what)
                except EnvError as e:
                    raise _invalid('/items/%d/release' % i, e.message)
                entry.setdefault('releases', {})[str(it['release'])] = r['id']
        return base

    def _scope_rows(self, fleet, device, service) -> str:
        rows = EnvStore(self.fleet.balena()).read(fleet, device, service)
        return value_sha256('\n'.join('%s\t%s' % (k, rows[k].get('value')) for k in sorted(rows)))

    def _plan(self, items: List[dict], fleets: Dict[str, dict], base: dict, policy: cc.Policy, required: int) -> dict:
        out = []
        p = self.fleet.patterns
        for i, it in enumerate(items):
            f = fleets[it['fleet']]
            if it['op'] == 'release.build':
                out.append({'op': it['op'], 'fleet': f['slug'], 'git': it['git'], 'finalize': True})
            elif it['op'] == 'env.patch':
                device = self.fleet.balena().device(it['device']) if it.get('device') else None
                out.append({'op': it['op'], 'fleet': f['slug'], 'device': it.get('device'),
                            'service': it['service'] or None,
                            'diff': self.fleet.env_diff(f, device, it['service'], it['set'], it['unset'])})
            else:
                devices = [normalize_device(d) for d in self.fleet.balena().devices(f['id'])]
                try:
                    waves = plan_waves(it['waves'], devices, lambda d: True, '/items/%d/waves' % i)
                except WaveError as e:
                    raise _invalid(e.pointer, e.message)
                entry = {'op': it['op'], 'fleet': f['slug'], 'device_count': len(devices),
                         'waves': [{'spec': s, 'devices': [d['name'] or d['uuid'][:7] for d in w]}
                                   for s, w in zip(it['waves'], waves)], 'soak_min': it['soak_min']}
                if it['op'] == 'release.deploy':
                    entry['from_release_id'] = base[f['slug']].get('target_release')
                    entry['to_release'] = it['release'] if it['release'] == BUILT else \
                        base[f['slug']]['releases'][str(it['release'])]
                    entry['halt_if'] = it['halt_if']
                else:
                    entry['site_change'] = {'title': it['change'].get('title'), 'ticket': it['change'].get('ticket'),
                                            'items': len(it['change']['items']),
                                            'signed': bool(it['change'].get('signature'))}
                out.append(entry)
        return {'items': out, 'policy': policy.to_dict(), 'required_approvals': required,
                'secret_names': sorted({n for it in items if it['op'] == 'env.patch' for n in it['set']
                                        if is_secret(n, p)})}

    # ── create ──
    def create(self, body: dict, principal, idempotency_key: Optional[str] = None,
               signed_body: Optional[dict] = None) -> Tuple[dict, bool]:
        """(change, created). An Idempotency-Key that was seen before returns that change."""
        if not isinstance(body, dict):
            raise _invalid('', 'must be a JSON object')
        cfg = self.cfg
        try:
            cc.check_caller(cfg, principal.kind)
        except cc.CallerError as e:
            raise ChangeError(403, 'caller-not-allowed', 'Caller kind not allowed', str(e))
        try:
            kid = verify_request(signed_body if signed_body is not None else body, cfg.trusted_keys,
                                 cfg.require_signature)
        except SignatureError as e:
            raise ChangeError(403, e.code, 'Change set signature rejected', e.detail)
        with _LOCK:
            if idempotency_key:
                db = self.session_factory()
                try:
                    existing = db.execute(select(ChangeSet).where(ChangeSet.idempotency_key == idempotency_key)
                                          ).scalar_one_or_none()
                finally:
                    db.close()
                if existing is not None:
                    return self.to_dict(existing), False
            items = self._normalize(body.get('items'))
            emergency = bool(body.get('emergency'))
            if emergency and not body.get('reason'):
                raise _invalid('/reason', 'an emergency change needs a reason')
            window = body.get('window', 'next')
            not_before = None
            if window not in ('next', None):
                try:
                    not_before = aware(datetime.fromisoformat(str(window).replace('Z', '+00:00')))
                except ValueError:
                    raise _invalid('/window', 'must be "next" or an RFC 3339 time to wait for')
            fleets = self._resolve(items)
            base = self._base(items, fleets)
            policy = cfg.combined_policy(RESOURCE_CLASS[it['op']] for it in items)
            required = cc.required_approvals(cfg, policy, emergency)
            plan = self._plan(items, fleets, base, policy, required)
            now = self.clock()
            change = ChangeSet(
                id=new_id('fchg'), title=str(body.get('title') or '')[:300] or None,
                ticket=str(body.get('ticket') or '')[:120] or None, state=cc.DRAFT,
                items=items, fleets=sorted(f['slug'] for f in fleets.values()), base=base, plan=plan,
                plan_hash=cc.plan_hash(base, items), policy=dict(policy.to_dict(), required_approvals=required),
                author_id=principal.sub, author_kind=principal.kind, signature_kid=kid, emergency=emergency,
                reason=body.get('reason'), window=str(window) if window else 'next', not_before=not_before,
                progress=None, idempotency_key=idempotency_key, created_at=now, updated_at=now)
            db = self.session_factory()
            try:
                db.add(change)
                db.commit()
            finally:
                db.close()
            self._event(change, cc.DRAFT, principal.sub, {'plan_hash': change.plan_hash})
            if emergency:
                self._emit('fleet.change.emergency', change)
            if not body.get('draft'):
                change = self._transition(change, cc.PLANNED, principal.sub)
                change = self._maybe_approved(change, principal.sub)
            return self.to_dict(change), True

    # ── reads ──
    def get(self, change_id: str) -> ChangeSet:
        db = self.session_factory()
        try:
            c = db.get(ChangeSet, change_id)
        finally:
            db.close()
        if c is None:
            raise ChangeError(404, 'not-found', 'No such change set')
        return c

    def list(self, state: Optional[str] = None, limit: int = 50) -> List[dict]:
        db = self.session_factory()
        try:
            q = select(ChangeSet).order_by(ChangeSet.created_at.desc()).limit(limit)
            if state:
                q = q.where(ChangeSet.state == state)
            return [self.to_dict(c, brief=True) for c in db.execute(q).scalars().all()]
        finally:
            db.close()

    def _approvals(self, change_id: str):
        db = self.session_factory()
        try:
            return db.execute(select(ChangeApproval).where(ChangeApproval.change_id == change_id)
                              .order_by(ChangeApproval.approved_at)).scalars().all()
        finally:
            db.close()

    def to_dict(self, c: ChangeSet, brief: bool = False) -> dict:
        p = self.fleet.patterns
        items = []
        for it in c.items or []:
            it = dict(it)
            if it['op'] == 'env.patch':
                it['set'] = {k: masked(k, v, p) for k, v in it['set'].items()}
            if it['op'] == 'site.change':
                it['change'] = {'title': it['change'].get('title'), 'ticket': it['change'].get('ticket'),
                                'items': len(it['change'].get('items') or []), 'signed': bool(it['change'].get('signature'))}
            items.append(it)
        out = {'id': c.id, 'title': c.title, 'ticket': c.ticket, 'state': c.state, 'fleets': c.fleets,
               'plan_hash': c.plan_hash, 'author': c.author_id, 'emergency': bool(c.emergency),
               'scheduled_for': iso(c.scheduled_for), 'window_end': iso(c.window_end), 'error': c.error,
               'created_at': iso(c.created_at), 'updated_at': iso(c.updated_at)}
        out.update(self._progress_summary(c))
        if brief:
            return out
        approvals = self._approvals(c.id)
        out.update({
            'items': items, 'plan': c.plan, 'policy': c.policy, 'reason': c.reason, 'window': c.window,
            'not_before': iso(c.not_before), 'signature_kid': c.signature_kid, 'applied_at': iso(c.applied_at),
            'finished_at': iso(c.finished_at), 'verify_result': c.verify_result,
            'approvals': [{'approver': a.approver_id, 'plan_hash': a.plan_hash, 'comment': a.comment,
                           'approved_at': iso(a.approved_at), 'counts': a.plan_hash == c.plan_hash}
                          for a in approvals],
            'progress': self._public_progress(c.progress), 'history': self._history(c.id),
        })
        return out

    def _progress_summary(self, c: ChangeSet) -> dict:
        prog = c.progress or {}
        out = {}
        for ip in prog.get('items', []):
            if ip.get('rollout'):
                out = rollout_summary(ip['rollout'])
            elif ip.get('fanout'):
                out = fanout_summary(ip['fanout'])
            if ip.get('state') == 'running':
                break
        for ip in prog.get('items', []):
            if ip.get('build_id'):
                out['build'] = {'id': ip['build_id'], 'state': ip.get('build_state'), 'release_id': ip.get('release_id')}
        return out

    @staticmethod
    def _public_progress(prog: Optional[dict]) -> Optional[dict]:
        if not prog:
            return prog
        prog = copy.deepcopy(prog)
        for ip in prog.get('items', []):
            ip.pop('previous', None)
            if ip.get('fanout'):
                ip['fanout'].pop('body', None)
        return prog

    def _history(self, change_id: str) -> List[dict]:
        db = self.session_factory()
        try:
            rows = db.execute(select(ChangeEvent).where(ChangeEvent.change_id == change_id)
                              .order_by(ChangeEvent.id)).scalars().all()
            return [{'ts': iso(r.ts), 'state': r.state, 'actor': r.actor, 'detail': r.detail} for r in rows]
        finally:
            db.close()

    # ── persistence helpers ──
    def _save(self, change: ChangeSet, **fields) -> ChangeSet:
        db = self.session_factory()
        try:
            row = db.get(ChangeSet, change.id)
            for k, v in fields.items():
                setattr(row, k, _jsonable(v) if k in ('progress', 'verify_result', 'plan', 'base') else v)
            row.updated_at = self.clock()
            db.commit()
            return row
        finally:
            db.close()

    def _event(self, change: ChangeSet, state: str, actor: Optional[str], detail=None):
        db = self.session_factory()
        try:
            db.add(ChangeEvent(change_id=change.id, ts=self.clock(), state=state, actor=actor or 'system:fleet-api',
                               detail=_jsonable(detail) if detail is not None else None))
            db.commit()
        finally:
            db.close()

    def _emit(self, event: str, change: ChangeSet):
        self.notifier.emit(event, {'change_id': change.id, 'state': change.state, 'ticket': change.ticket,
                                   'title': change.title, 'fleets': change.fleets, 'plan_hash': change.plan_hash,
                                   'error': change.error, 'url': '/fleet/v1/changes/%s' % change.id})

    def _transition(self, change: ChangeSet, to: str, actor: Optional[str] = None, detail=None, **fields) -> ChangeSet:
        cc.check_transition(change.state, to)
        if to in cc.TERMINAL:
            fields.setdefault('finished_at', self.clock())
        change = self._save(change, state=to, **fields)
        self._event(change, to, actor, detail)
        self.audit.write(None, 'fleet.change.%s' % to, target='/fleet/v1/changes/%s' % change.id,
                         after={'ticket': change.ticket, 'fleets': change.fleets, 'detail': _jsonable(detail)},
                         actor_sub=actor or 'system:fleet-api', actor_kind='system' if not actor else 'user',
                         outcome='ok')
        self._emit('fleet.change.%s' % to, change)
        return change

    # ── approvals and manual transitions ──
    def _maybe_approved(self, change: ChangeSet, actor: Optional[str]) -> ChangeSet:
        required = (change.policy or {}).get('required_approvals', 0)
        pairs = [(a.approver_id, a.plan_hash) for a in self._approvals(change.id)]
        if change.state == cc.PLANNED and cc.approvals_met(pairs, change.plan_hash, required):
            change = self._transition(change, cc.APPROVED, actor)
            change = self.advance(change, self.clock())
        return change

    def _base_moved(self, change: ChangeSet) -> bool:
        try:
            fleets = self._resolve(change.items)
            return self._base(change.items, fleets) != change.base
        except ChangeError:
            return True

    def approve(self, change_id: str, principal, plan_hash: Optional[str], comment: Optional[str] = None) -> dict:
        with _LOCK:
            change = self.get(change_id)
            approved_by = [a.approver_id for a in self._approvals(change_id) if a.plan_hash == change.plan_hash]
            try:
                cc.check_approval(change.state, change.author_id, principal.sub, change.plan_hash, plan_hash,
                                  approved_by)
            except cc.ApprovalError as e:
                raise ChangeError(409 if e.code != 'author-cannot-approve' else 403, e.code, 'Approval refused',
                                  e.detail)
            if self._base_moved(change):
                self._transition(change, cc.STALE, principal.sub, {'reason': 'base state moved'})
                raise ChangeError(409, 'stale', 'The plan is stale',
                                  'The fleet changed since this plan was made: re-plan it (POST .../plan).')
            db = self.session_factory()
            try:
                db.add(ChangeApproval(change_id=change_id, approver_id=principal.sub, plan_hash=plan_hash,
                                      comment=comment, approved_at=self.clock()))
                db.commit()
            finally:
                db.close()
            self._event(change, change.state, principal.sub, {'approved': plan_hash})
            change = self._maybe_approved(self.get(change_id), principal.sub)
            return self.to_dict(change)

    def reject(self, change_id: str, principal, reason: Optional[str]) -> dict:
        with _LOCK:
            change = self.get(change_id)
            if change.state not in (cc.PLANNED, cc.APPROVED, cc.SCHEDULED):
                raise ChangeError(409, 'not-rejectable', 'Change set cannot be rejected', 'it is %s' % change.state)
            return self.to_dict(self._transition(change, cc.REJECTED, principal.sub, {'reason': reason},
                                                 error='rejected: %s' % (reason or 'no reason given')))

    def cancel(self, change_id: str, principal, reason: Optional[str]) -> dict:
        with _LOCK:
            change = self.get(change_id)
            if cc.CANCELLED not in cc.TRANSITIONS.get(change.state, ()):
                raise ChangeError(409, 'not-cancellable', 'Change set cannot be cancelled', 'it is %s' % change.state)
            return self.to_dict(self._transition(change, cc.CANCELLED, principal.sub, {'reason': reason}))

    def replan(self, change_id: str, principal) -> dict:
        with _LOCK:
            change = self.get(change_id)
            if change.state not in (cc.DRAFT, cc.STALE, cc.PLANNED):
                raise ChangeError(409, 'not-replannable', 'Change set cannot be re-planned', 'it is %s' % change.state)
            fleets = self._resolve(change.items)
            base = self._base(change.items, fleets)
            policy = cc.Policy(change.policy['approvals'], change.policy['window'])
            plan = self._plan(change.items, fleets, base, policy, change.policy['required_approvals'])
            new_hash = cc.plan_hash(base, change.items)
            change = self._save(change, base=base, plan=plan, plan_hash=new_hash)
            if change.state != cc.PLANNED:
                change = self._transition(change, cc.PLANNED, principal.sub, {'plan_hash': new_hash})
            else:
                self._event(change, cc.PLANNED, principal.sub, {'plan_hash': new_hash, 'replanned': True})
            return self.to_dict(self._maybe_approved(change, principal.sub))

    def apply_now(self, change_id: str, principal) -> dict:
        with _LOCK:
            try:
                cc.check_caller(self.cfg, principal.kind)
            except cc.CallerError as e:
                raise ChangeError(403, 'caller-not-allowed', 'Caller kind not allowed', str(e))
            change = self.get(change_id)
            if change.state not in (cc.APPROVED, cc.SCHEDULED):
                raise ChangeError(409, 'not-applicable', 'Change set cannot be applied', 'it is %s' % change.state)
            d = self._decide(change, self.clock())
            if d.action != 'apply':
                raise ChangeError(409, 'window-closed', 'Outside a release window', d.reason)
            if self._busy(change):
                raise ChangeError(409, 'fleet-busy', 'Another change is rolling out on this fleet')
            return self.to_dict(self.advance(change, self.clock(), actor=principal.sub))

    def request_rollback(self, change_id: str, principal, reason: Optional[str]) -> dict:
        with _LOCK:
            change = self.get(change_id)
            if change.state not in (cc.APPLIED, cc.APPLYING, cc.VERIFYING):
                raise ChangeError(409, 'not-rollbackable', 'Change set cannot be rolled back', 'it is %s' % change.state)
            return self.to_dict(self._rollback(change, 'rollback requested: %s' % (reason or 'no reason given'),
                                               principal.sub))

    # ── scheduling ──
    def _decide(self, change: ChangeSet, now: datetime) -> cc.Decision:
        policy = cc.Policy(change.policy['approvals'], change.policy['window'])
        return cc.decide(self.cfg, policy, now, emergency=bool(change.emergency), not_before=aware(change.not_before),
                         scheduled_window_end=aware(change.window_end) if change.state == cc.SCHEDULED else None,
                         schedule=Schedule(self.cfg))

    def _busy(self, change: ChangeSet) -> bool:
        db = self.session_factory()
        try:
            active = db.execute(select(ChangeSet).where(ChangeSet.state.in_([cc.APPLYING, cc.VERIFYING]),
                                                        ChangeSet.id != change.id)).scalars().all()
        finally:
            db.close()
        mine = set(change.fleets or [])
        return any(mine & set(c.fleets or []) for c in active)

    def fleet_busy(self, slug: str) -> Optional[str]:
        db = self.session_factory()
        try:
            for c in db.execute(select(ChangeSet).where(ChangeSet.state.in_([cc.APPLYING, cc.VERIFYING]))).scalars():
                if slug in (c.fleets or []):
                    return c.id
        finally:
            db.close()
        return None

    def tick(self):
        """One scheduler pass over every active change, oldest first."""
        with _LOCK:
            db = self.session_factory()
            try:
                ids = [c.id for c in db.execute(select(ChangeSet).where(ChangeSet.state.in_(list(cc.ACTIVE)))
                                                .order_by(ChangeSet.created_at)).scalars()]
            finally:
                db.close()
            for cid in ids:
                try:
                    self.advance(self.get(cid), self.clock())
                except (BalenaError, OSError) as e:
                    log.warning('change %s: %s (retrying next tick)', cid, e)
                except Exception:
                    log.exception('change %s: scheduler step failed', cid)

    def advance(self, change: ChangeSet, now: datetime, actor: Optional[str] = None) -> ChangeSet:
        with _LOCK:
            if change.state in (cc.APPROVED, cc.SCHEDULED):
                d = self._decide(change, now)
                if d.action == 'expire':
                    return self._transition(change, cc.EXPIRED, actor, {'reason': d.reason}, error=d.reason)
                if d.action == 'wait':
                    if change.state == cc.APPROVED:
                        return self._transition(change, cc.SCHEDULED, actor, {'reason': d.reason},
                                                scheduled_for=d.scheduled_for, window_end=d.window_end)
                    if d.scheduled_for and aware(change.scheduled_for) != d.scheduled_for:
                        change = self._save(change, scheduled_for=d.scheduled_for, window_end=d.window_end)
                    return change
                if self._busy(change):
                    if change.state == cc.APPROVED and d.window_end:
                        return self._transition(change, cc.SCHEDULED, actor, {'reason': 'queued behind another change'},
                                                scheduled_for=d.scheduled_for, window_end=d.window_end)
                    return change
                if self._base_moved(change):
                    return self._transition(change, cc.STALE, actor, {'reason': 'base state moved'})
                change = self._transition(change, cc.APPLYING, actor, {'reason': d.reason}, applied_at=now,
                                          window_end=d.window_end, progress=self._initial_progress(change))
            if change.state == cc.APPLYING:
                change = self._run_items(change, now)
            if change.state == cc.VERIFYING:
                change = self._verify(change, now)
            return change

    # ── applying ──
    def _initial_progress(self, change: ChangeSet) -> dict:
        reach = self.fleet.reach()
        baseline = {}
        for slug in sorted({it['fleet'] for it in change.items if it['op'] == 'env.patch'}):
            f = self.fleet.fleet(slug)
            for d in (normalize_device(x) for x in self.fleet.balena().devices(f['id'])):
                if d['online']:
                    baseline.setdefault(slug, {})[d['uuid']] = reach.health(d).to_dict()
        return {'cursor': 0, 'items': [{'op': it['op'], 'state': 'pending'} for it in change.items],
                'built': {}, 'baseline': baseline}

    def _run_items(self, change: ChangeSet, now: datetime) -> ChangeSet:
        prog = copy.deepcopy(change.progress)
        actor = 'change:%s' % change.id
        while prog['cursor'] < len(change.items):
            i = prog['cursor']
            it, ip = change.items[i], prog['items'][i]
            fleet = self.fleet.fleet(it['fleet'])
            if it['op'] == 'release.build':
                if ip['state'] == 'pending':
                    b = self.builds.start(fleet, it['git'], actor, change_id=change.id)
                    ip.update(state='running', build_id=b['id'], build_state=b['status'])
                    change = self._save(change, progress=prog)
                b = self.builds.get(ip['build_id'])
                ip['build_state'] = b.state
                if b.state == 'success':
                    ip.update(state='done', release_id=b.release_id, commit=b.commit)
                    prog['built'][fleet['slug']] = b.release_id
                elif b.state == 'failed':
                    ip['state'] = 'failed'
                    change = self._save(change, progress=prog)
                    return self._fail(change, 'release build %s failed: %s' % (b.id, b.error))
                else:
                    return self._save(change, progress=prog)
            elif it['op'] == 'env.patch':
                device = self.fleet.device(it['device']) if it.get('device') else None
                res = self.fleet.patch_env(fleet, device, it['service'], it['set'], it['unset'], actor, change.id)
                ip.update(state='done', previous=res['_previous'], changed=res['changed'])
            elif it['op'] == 'release.deploy':
                if ip['state'] == 'pending':
                    rid = prog['built'].get(fleet['slug']) if it['release'] == BUILT \
                        else change.base[fleet['slug']]['releases'][str(it['release'])]
                    release = self.fleet.balena().release(rid)
                    if release is None or release.get('status') != 'success' or not release.get('is_final'):
                        ip['state'] = 'failed'
                        change = self._save(change, progress=prog)
                        return self._fail(change, 'release %s is not a final, successful release' % rid)
                    ip.update(state='running', release_id=rid,
                              rollout=self.rollout.start(fleet, rid, it['waves'], it['soak_min'], it['halt_if'], now))
                ip['rollout'] = self.rollout.tick(ip['rollout'], now)
                status = ip['rollout']['status']
                if status == 'halted':
                    ip['state'] = 'halted'
                    change = self._save(change, progress=prog)
                    return self._rollback(change, 'rollout halted: %s' % ip['rollout']['halt_reason'], None)
                if status != 'done':
                    return self._save(change, progress=prog)
                ip['state'] = 'done'
            else:
                if ip['state'] == 'pending':
                    ip.update(state='running', fanout=self.fanout.start(fleet, change.id, it['change'], it['waves'],
                                                                          it['soak_min']))
                    ip['fanout']['timeout_min'] = it.get('timeout_min')
                ip['fanout'] = self.fanout.tick(ip['fanout'], now)
                if ip['fanout']['status'] == 'halted':
                    ip['state'] = 'halted'
                    change = self._save(change, progress=prog)
                    return self._fail(change, 'site change fan-out halted: %s' % ip['fanout']['halt_reason'])
                if ip['fanout']['status'] != 'done':
                    return self._save(change, progress=prog)
                ip['state'] = 'done'
            prog['cursor'] += 1
            change = self._save(change, progress=prog)
        prog['verify_started'] = iso(now)
        change = self._save(change, progress=prog)
        return self._transition(change, cc.VERIFYING, None)

    def _verify(self, change: ChangeSet, now: datetime) -> ChangeSet:
        prog = change.progress
        reach = self.fleet.reach()
        pending, failures = [], []
        expected: Dict[str, dict] = {}
        for it, ip in zip(change.items, prog['items']):
            if it['op'] == 'release.deploy' and ip.get('rollout'):
                st = ip['rollout']
                expected[st['fleet']] = {'release_id': st['release_id'], 'move_fleet': st['move_fleet'],
                                         'devices': set(st['devices']), 'baseline': st['baseline']}
        checked = sorted(set(expected) | set(prog.get('baseline', {})))
        for slug in checked:
            f = self.fleet.fleet(slug)
            exp = expected.get(slug)
            env_base = prog.get('baseline', {}).get(slug, {})
            for d in (normalize_device(x) for x in self.fleet.balena().devices(f['id'])):
                if not d['online']:
                    continue
                # Devices the rollout pinned, or every unpinned device once the fleet itself moved.
                must_run = exp and (d['uuid'] in exp['devices'] or (exp['move_fleet'] and d['pinned_release_id'] is None))
                if must_run and d['release_id'] != exp['release_id']:
                    pending.append('%s not on release %s yet' % (d['name'] or d['uuid'][:7], exp['release_id']))
                    continue
                base = (exp['baseline'].get(d['uuid']) if exp else None) or env_base.get(d['uuid'])
                h = reach.health(d)
                if worse(h, base):
                    failures.append('%s health %s -> %s' % (d['name'] or d['uuid'][:7], base.get('status'), h.status))
        result = {'checked_fleets': checked, 'pending': pending, 'failures': failures, 'at': iso(now)}
        if failures:
            change = self._save(change, verify_result=result)
            return self._rollback(change, 'verification failed: %s' % '; '.join(failures), None)
        started = datetime.fromisoformat(prog['verify_started'])
        settle = timedelta(seconds=max(int(self.settings()['rollout'].get('verify_settle_s', 60)),
                                       self.cfg.verify_settle_s))
        if not pending:
            if now - started < settle:
                return self._save(change, verify_result=dict(result, settling_until=iso(started + settle)))
            change = self._save(change, verify_result=dict(result, passed=True))
            return self._transition(change, cc.APPLIED, None, {'verify': 'passed'})
        if now - started >= timedelta(seconds=self.cfg.verify_timeout_s):
            change = self._save(change, verify_result=result)
            return self._rollback(change, 'verification timed out: %s' % '; '.join(pending), None)
        return self._save(change, verify_result=result)

    # ── failure and rollback ──
    def _fail(self, change: ChangeSet, reason: str) -> ChangeSet:
        prog = change.progress or {}
        mutated = any(ip.get('state') in ('done', 'running', 'halted') and ip['op'] in ('env.patch', 'release.deploy')
                      for ip in prog.get('items', []))
        if mutated:
            return self._rollback(change, reason, None)
        return self._transition(change, cc.FAILED, None, {'reason': reason}, error=reason)

    def _rollback(self, change: ChangeSet, reason: str, actor: Optional[str]) -> ChangeSet:
        prog = copy.deepcopy(change.progress or {'items': []})
        undo_errors = []
        for it, ip in reversed(list(zip(change.items, prog.get('items', [])))):
            try:
                if it['op'] == 'release.deploy' and ip.get('rollout') and ip['rollout']['status'] not in ('halted',
                                                                                                         'reverted'):
                    self.rollout.revert(ip['rollout'])
                    ip['rollout']['status'] = 'reverted'
                    ip['state'] = 'reverted'
                elif it['op'] == 'env.patch' and ip.get('state') == 'done' and ip.get('previous') is not None:
                    fleet = self.fleet.fleet(it['fleet'])
                    device = self.fleet.device(it['device']) if it.get('device') else None
                    self.fleet.restore_env(fleet, device, it['service'], ip['previous'], 'change:%s' % change.id,
                                           change.id)
                    ip['state'] = 'reverted'
            except (BalenaError, NotFound, EnvError) as e:
                undo_errors.append('%s: %s' % (it['op'], e))
        change = self._save(change, progress=prog)
        detail = {'reason': reason}
        if undo_errors:
            detail['undo_errors'] = undo_errors
        return self._transition(change, cc.ROLLED_BACK, actor, detail,
                                 error=reason + ('; undo errors: %s' % '; '.join(undo_errors) if undo_errors else ''))
