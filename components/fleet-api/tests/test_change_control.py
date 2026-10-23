"""metrolla_change_control: plan hash, signatures (metrolla-cli spec), windows, freezes,
lifecycle and approval rules. No balena anywhere in here."""
import base64
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import metrolla_change_control as cc
from metrolla_change_control import Schedule, SignatureError, sign_change, verify_change, verify_request

NY = ZoneInfo('America/New_York')


def test_module_imports_nothing_host_specific():
    """It must be able to move into metrolla_auth: stdlib, dateutil and cryptography only."""
    import ast
    import pathlib
    allowed = {'base64', 'dataclasses', 'datetime', 'hashlib', 'json', 'typing', 'zoneinfo', 'dateutil', 'cryptography'}
    for path in pathlib.Path(cc.__file__).parent.glob('*.py'):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module]
            else:
                continue
            for n in names:
                assert n.split('.')[0] in allowed, '%s imports %s' % (path.name, n)


def test_canonical_json_matches_the_cli():
    body = {'b': [1, {'z': 'é', 'a': None}], 'a': True}
    assert cc.canonical_json(body) == '{"a":true,"b":[1,{"a":null,"z":"é"}]}'.encode('utf-8')
    with pytest.raises(ValueError):
        cc.canonical_json({'x': float('nan')})


def test_plan_hash_is_deterministic_and_ignores_key_order():
    items = [{'op': 'env.patch', 'fleet': 'f', 'set': {'A': '1', 'B': '2'}}]
    h1 = cc.plan_hash({'f': {'target_release': 1}}, items)
    h2 = cc.plan_hash({'f': {'target_release': 1}}, [{'set': {'B': '2', 'A': '1'}, 'fleet': 'f', 'op': 'env.patch'}])
    assert h1 == h2 and h1.startswith('sha256:') and len(h1) == 7 + 64
    assert cc.plan_hash({'f': {'target_release': 2}}, items) != h1
    assert cc.plan_hash({'f': {'target_release': 1}}, items + [{'op': 'x'}]) != h1


# ── signatures, exactly as metrolla-cli's README "Change set signatures" ──
def _cli_sign(body, kid, key):
    """An independent implementation of the documented signing steps."""
    def canon(x):
        return json.dumps(x, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()

    def b64(b):
        return base64.urlsafe_b64encode(b).rstrip(b'=').decode()
    payload = canon({k: v for k, v in body.items() if k != 'signature'})
    header = canon({'alg': 'EdDSA', 'kid': kid})
    inp = b64(header) + '.' + b64(payload)
    return {'kid': kid, 'jws': inp + '.' + b64(key.sign(inp.encode('ascii')))}


@pytest.fixture
def keypair():
    key = Ed25519PrivateKey.generate()
    return key, {'ci-2026': cc.public_key_b64(key)}


def test_signature_verifies_and_matches_the_reference_signer(keypair):
    key, trusted = keypair
    body = {'title': 't', 'ticket': 'CHG1', 'items': [{'op': 'env.patch', 'fleet': 'f', 'set': {'A': 'ü'}}],
            'window': 'next'}
    body['signature'] = _cli_sign(body, 'ci-2026', key)
    assert verify_change(body, trusted) == 'ci-2026'
    assert sign_change({k: v for k, v in body.items() if k != 'signature'}, 'ci-2026', key)['jws'].split('.')[:2] \
        == body['signature']['jws'].split('.')[:2]
    assert trusted['ci-2026'].startswith('MCowBQYDK2VwAyEA')


@pytest.mark.parametrize('mutate,code', [
    (lambda b: b.update(ticket='CHG2'), 'signature-mismatch'),
    (lambda b: b['signature'].update(kid='other'), 'signature-malformed'),
    (lambda b: b['signature'].update(jws='a.b'), 'signature-malformed'),
])
def test_tampered_signatures_are_refused(keypair, mutate, code):
    key, trusted = keypair
    body = {'title': 't', 'items': []}
    body['signature'] = _cli_sign(body, 'ci-2026', key)
    mutate(body)
    with pytest.raises(SignatureError) as e:
        verify_change(body, trusted)
    assert e.value.code == code


def test_unknown_key_wrong_alg_and_wrong_key(keypair):
    key, trusted = keypair
    body = {'items': []}
    body['signature'] = _cli_sign(body, 'ci-old', key)
    with pytest.raises(SignatureError) as e:
        verify_change(body, trusted)
    assert e.value.code == 'unknown-key'

    other = Ed25519PrivateKey.generate()
    body['signature'] = _cli_sign({'items': []}, 'ci-2026', other)
    with pytest.raises(SignatureError) as e:
        verify_change(body, trusted)
    assert e.value.code == 'signature-invalid'

    hdr = cc.b64url(cc.canonical_json({'alg': 'HS256', 'kid': 'ci-2026'}))
    parts = _cli_sign({'items': []}, 'ci-2026', key)['jws'].split('.')
    body['signature'] = {'kid': 'ci-2026', 'jws': '.'.join([hdr] + parts[1:])}
    with pytest.raises(SignatureError) as e:
        verify_change(body, trusted)
    assert e.value.code == 'signature-malformed'


def test_verify_request_required_and_optional(keypair):
    _, trusted = keypair
    assert verify_request({'items': []}, trusted, require=False) is None
    with pytest.raises(SignatureError) as e:
        verify_request({'items': []}, trusted, require=True)
    assert e.value.code == 'signature-required'


# ── config ──
def test_config_validation_reports_pointers():
    with pytest.raises(cc.ChangeControlConfigError) as e:
        cc.parse_change_control({'mode': 'strict', 'windows': [{'rrule': 'FREQ=NOPE', 'duration_min': 5}],
                                 'policy': {'env': {'approvals': -1}}, 'require_signature': True,
                                 'trusted_keys': [{'kid': 'k', 'ed25519': 'bm90IGEga2V5'}], 'bogus': 1})
    pointers = {x['pointer'] for x in e.value.errors}
    assert {'/mode', '/windows/0/rrule', '/policy/env/approvals', '/trusted_keys/0/ed25519', '/bogus',
            '/require_signature'} <= pointers


def test_policy_lookup_falls_back_to_parent_class_then_default():
    cfg = cc.parse_change_control({'policy': {'op.device': {'approvals': 1}, 'default': {'approvals': 0,
                                                                                         'window': True}}})
    assert cfg.policy_for('op.device.reboot') == cc.Policy(1, False)
    assert cfg.policy_for('env') == cc.Policy(0, True)
    assert cfg.combined_policy(['op.device.reboot', 'env']) == cc.Policy(1, True)
    assert cc.parse_change_control({}).mode == 'open'


# ── windows and freezes ──
def _cfg(**kw):
    base = {'timezone': 'America/New_York',
            'windows': [{'name': 'weekly', 'rrule': 'FREQ=WEEKLY;BYDAY=TU,TH;BYHOUR=2', 'duration_min': 120}]}
    base.update(kw)
    return cc.parse_change_control(base)


def test_weekly_window_in_site_time_zone_across_dst():
    s = Schedule(_cfg())
    # Tue 2026-11-03 01:30 EST: closed; opens 02:00 local.
    t = datetime(2026, 11, 3, 1, 30, tzinfo=NY)
    assert s.window_at(t) is None
    start, end = s.next_open(t)
    assert start == datetime(2026, 11, 3, 2, 0, tzinfo=NY) and end - start == timedelta(hours=2)
    assert s.window_at(datetime(2026, 11, 3, 3, 59, tzinfo=NY)) is not None
    assert s.window_at(datetime(2026, 11, 3, 4, 0, tzinfo=NY)) is None
    # Before the DST change (EDT, UTC-4) the same wall-clock hour is a different UTC hour.
    start, _ = s.next_open(datetime(2026, 10, 27, 0, 0, tzinfo=NY))
    assert start.astimezone(timezone.utc).hour == 6
    start, _ = s.next_open(datetime(2026, 11, 3, 0, 0, tzinfo=NY))
    assert start.astimezone(timezone.utc).hour == 7


def test_freeze_beats_window():
    s = Schedule(_cfg(freezes=[{'start': '2026-11-25T00:00:00-05:00', 'end': '2026-12-01T00:00:00-05:00',
                                'reason': 'peak'}]))
    thu_in_freeze = datetime(2026, 11, 26, 2, 30, tzinfo=NY)
    assert s.window_at(thu_in_freeze) is None
    assert s.frozen(thu_in_freeze).reason == 'peak'
    start, _ = s.next_open(thu_in_freeze)
    assert start == datetime(2026, 12, 1, 2, 0, tzinfo=NY)


def test_no_windows_means_open_except_freezes():
    s = Schedule(cc.parse_change_control({'freezes': [{'start': '2026-11-25T00:00:00Z',
                                                       'end': '2026-11-26T00:00:00Z'}]}))
    assert s.window_at(datetime(2026, 11, 24, tzinfo=timezone.utc))
    assert s.window_at(datetime(2026, 11, 25, 12, tzinfo=timezone.utc)) is None
    assert s.next_open(datetime(2026, 11, 25, 12, tzinfo=timezone.utc))[0] == datetime(2026, 11, 26, tzinfo=timezone.utc)


def test_decide_apply_wait_expire_and_emergency():
    cfg = _cfg()
    windowed, free = cc.Policy(1, True), cc.Policy(0, False)
    closed = datetime(2026, 11, 3, 1, 0, tzinfo=NY)
    d = cc.decide(cfg, windowed, closed)
    assert d.action == 'wait' and d.scheduled_for == datetime(2026, 11, 3, 2, 0, tzinfo=NY)
    assert cc.decide(cfg, windowed, closed, emergency=True).action == 'apply'
    assert cc.decide(cfg, free, closed).action == 'apply'
    assert cc.decide(cfg, windowed, datetime(2026, 11, 3, 2, 30, tzinfo=NY)).action == 'apply'
    # Scheduled for the Tuesday window, not started by its end: expired, even though Thursday opens later.
    d = cc.decide(cfg, windowed, datetime(2026, 11, 5, 2, 30, tzinfo=NY),
                  scheduled_window_end=datetime(2026, 11, 3, 4, 0, tzinfo=NY))
    assert d.action == 'expire'
    later = datetime(2026, 11, 3, 1, 0, tzinfo=NY) + timedelta(days=30)
    assert cc.decide(cfg, free, closed, not_before=later).action == 'wait'


# ── lifecycle and approvals ──
def test_transitions():
    cc.check_transition(cc.PLANNED, cc.APPROVED)
    cc.check_transition(cc.SCHEDULED, cc.EXPIRED)
    cc.check_transition(cc.APPLIED, cc.ROLLED_BACK)
    for frm, to in [(cc.APPLIED, cc.APPLYING), (cc.CANCELLED, cc.PLANNED), (cc.DRAFT, cc.APPROVED),
                    (cc.APPLYING, cc.CANCELLED)]:
        with pytest.raises(cc.TransitionError):
            cc.check_transition(frm, to)


def test_author_cannot_approve_and_hash_binding():
    h = 'sha256:' + 'a' * 64
    with pytest.raises(cc.ApprovalError) as e:
        cc.check_approval(cc.PLANNED, 'ci', 'ci', h, h, [])
    assert e.value.code == 'author-cannot-approve'
    with pytest.raises(cc.ApprovalError) as e:
        cc.check_approval(cc.PLANNED, 'ci', 'bob', h, 'sha256:' + 'b' * 64, [])
    assert e.value.code == 'plan-hash-mismatch'
    with pytest.raises(cc.ApprovalError) as e:
        cc.check_approval(cc.PLANNED, 'ci', 'bob', h, h, ['bob'])
    assert e.value.code == 'already-approved'
    with pytest.raises(cc.ApprovalError) as e:
        cc.check_approval(cc.APPLIED, 'ci', 'bob', h, h, [])
    assert e.value.code == 'not-approvable'
    cc.check_approval(cc.PLANNED, 'ci', 'bob', h, h, [])
    old = 'sha256:' + 'c' * 64
    assert not cc.approvals_met([('bob', old), ('amy', h)], h, 2)
    assert cc.approvals_met([('bob', h), ('amy', h), ('amy', h)], h, 2)


def test_callers_and_gated_direct_writes():
    cfg = cc.parse_change_control({'mode': 'gated', 'allowed_callers': ['machine'],
                                   'policy': {'env': {'approvals': 1}, 'op.device': {'approvals': 0}}})
    with pytest.raises(cc.CallerError):
        cc.check_caller(cfg, 'interactive')
    cc.check_caller(cfg, 'machine')
    assert not cc.direct_write_allowed(cfg, 'env')
    assert cc.direct_write_allowed(cfg, 'op.device.reboot')
    assert cc.direct_write_allowed(cc.parse_change_control({}), 'env')
    assert cc.required_approvals(cfg, cc.Policy(1, True), emergency=True) == 2
