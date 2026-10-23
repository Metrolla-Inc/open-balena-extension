"""Fleet change-set lifecycle through the API: plan-hash approvals, separation of duties,
windows, freezes, expiry, staleness, signatures, idempotency, gated direct writes."""
from datetime import datetime, timezone

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from conftest import FLEET, FLEET_SEG, as_, auth_on
from metrolla_auth import change_control as cc
from metrolla_auth.change_control import public_key_b64, sign_change

WINDOWS = [{'name': 'weekly', 'rrule': 'FREQ=WEEKLY;BYDAY=TU,TH;BYHOUR=2', 'duration_min': 120}]


def _env_item(value='new'):
    return {'op': 'env.patch', 'fleet': FLEET, 'set': {'MODE': value}}


def _deploy(release_id):
    return {'op': 'release.deploy', 'fleet': FLEET, 'release': release_id, 'waves': [{'percent': 100}]}


def _post(world, items, who='ci', **extra):
    return world.client.post('/fleet/v1/changes', json=dict({'ticket': 'CHG1', 'items': items}, **extra),
                             headers=as_(who))


def _approve(world, cid, who, plan_hash):
    return world.client.post('/fleet/v1/changes/%s/approve' % cid, json={'plan_hash': plan_hash}, headers=as_(who))


def test_two_approvals_bound_to_the_plan_hash_and_author_cannot_approve(world):
    world.start(change_control={'policy': {'release.deploy': {'approvals': 2}}}, auth=auth_on())
    r2 = world.balena.release(world.fleet)
    c = _post(world, [_deploy(r2['id'])]).json
    assert c['state'] == 'planned' and c['policy']['required_approvals'] == 2
    h = c['plan_hash']

    r = _approve(world, c['id'], 'ci', h)
    assert r.status_code == 403 and r.json['type'].endswith('forbidden'), 'change_pipeline cannot approve at all'
    own = _post(world, [_deploy(r2['id'])], who='root').json
    r = _approve(world, own['id'], 'root', own['plan_hash'])
    assert r.status_code == 403 and r.json['type'].endswith('author-cannot-approve')
    r = _approve(world, c['id'], 'amy', 'sha256:' + '0' * 64)
    assert r.status_code == 409 and r.json['type'].endswith('plan-hash-mismatch')
    assert _approve(world, c['id'], 'amy', h).json['state'] == 'planned'
    assert _approve(world, c['id'], 'amy', h).status_code == 409
    r = _approve(world, c['id'], 'bob', h)
    assert r.status_code == 200 and r.json['state'] == 'applying'
    assert [a['approver'] for a in r.json['approvals']] == ['static:amy', 'static:bob']
    assert r.json['author'] == 'static:ci'


def test_window_schedules_then_applies_and_a_missed_window_expires(world):
    world.start(change_control={'timezone': 'America/New_York', 'windows': WINDOWS,
                                'policy': {'env': {'approvals': 0, 'window': True}}}, auth=auth_on())
    # 2026-11-03 15:00Z is Tue 10:00 EST: the Tuesday window is over; next is Thu 02:00 EST.
    c = _post(world, [_env_item()]).json
    assert c['state'] == 'scheduled' and c['scheduled_for'] == '2026-11-05T07:00:00+00:00'
    world.tick(minutes=60)
    assert world.change(c['id'])['state'] == 'scheduled'
    world.clock.now = datetime(2026, 11, 5, 7, 30, tzinfo=timezone.utc)
    world.tick()
    assert world.change(c['id'])['state'] == 'applied'
    assert world.balena.env('application_environment_variable', 'application', world.fleet['id']) == {'MODE': 'new'}

    late = _post(world, [_env_item('later')]).json             # Thu 07:30Z: window open now
    assert late['state'] == 'applied'
    world.clock.now = datetime(2026, 11, 5, 9, 30, tzinfo=timezone.utc)    # Thu 04:30 EST, closed
    missed = _post(world, [_env_item('missed')]).json
    assert missed['scheduled_for'] == '2026-11-10T07:00:00+00:00'
    world.clock.now = datetime(2026, 11, 10, 9, 30, tzinfo=timezone.utc)   # scheduler was down all window
    world.tick()
    m = world.change(missed['id'])
    assert m['state'] == 'expired' and 'window ended' in m['error']
    assert 'fleet.change.expired' in world.hooks.types()


def test_change_control_webhooks_are_signed_like_edgeapi(world):
    world.start(change_control={'webhooks': [{'url': world.hooks.url, 'secret': 's3',
                                              'events': ['fleet.change.applied']}]}, webhooks=[])
    c = _post(world, [_env_item()]).json
    assert c['state'] == 'applied'
    assert world.hooks.types() == ['fleet.change.applied']
    hit = world.hooks.events[0]
    assert hit['body']['change_id'] == c['id'] and hit['body']['ticket'] == 'CHG1'
    assert cc.verify_signature('s3', hit['raw'], hit['headers'][cc.SIGNATURE_HEADER])
    assert hit['headers']['X-Metrolla-Delivery']
    shown = world.client.get('/fleet/v1/change-control', headers=world.headers).json
    assert shown['webhooks'] == [{'url': world.hooks.url, 'events': ['fleet.change.applied'], 'signed': True}]


def test_freeze_beats_window_and_emergency_bypasses_both(world):
    world.start(change_control={
        'timezone': 'America/New_York', 'windows': WINDOWS, 'emergency': {'approvals': 2},
        'freezes': [{'start': '2026-11-04T00:00:00-05:00', 'end': '2026-11-09T00:00:00-05:00', 'reason': 'peak'}],
        'policy': {'env': {'approvals': 0, 'window': True}}}, auth=auth_on())
    c = _post(world, [_env_item()]).json
    assert c['scheduled_for'] == '2026-11-10T07:00:00+00:00', 'Thursday is frozen'
    cc_view = world.client.get('/fleet/v1/change-control', headers=as_('root')).json
    assert cc_view['schedule']['next_window']['start'] == '2026-11-10T02:00:00-05:00'

    assert world.client.post('/fleet/v1/changes', json={'items': [_env_item('x')], 'emergency': True},
                             headers=as_('ci')).status_code == 422, 'emergency needs a reason'
    e = _post(world, [_env_item('hotfix')], emergency=True, reason='gate stuck open').json
    assert e['state'] == 'planned' and e['policy']['required_approvals'] == 2
    _approve(world, e['id'], 'amy', e['plan_hash'])
    e = _approve(world, e['id'], 'bob', e['plan_hash']).json
    assert e['state'] == 'applied'
    assert 'fleet.change.emergency' in world.hooks.types()


def test_base_moved_makes_the_plan_stale_and_replan_voids_approvals(world):
    world.start(change_control={'policy': {'release.deploy': {'approvals': 1}}}, auth=auth_on())
    r2, r3 = world.balena.release(world.fleet), world.balena.release(world.fleet)
    c = _post(world, [_deploy(r2['id'])]).json
    old = c['plan_hash']
    world.balena.pin_fleet(world.fleet, r3)                     # someone used the dashboard
    r = _approve(world, c['id'], 'amy', old)
    assert r.status_code == 409 and r.json['type'].endswith('stale')
    assert world.change(c['id'])['state'] == 'stale'
    c = world.client.post('/fleet/v1/changes/%s/plan' % c['id'], headers=as_('ci')).json
    assert c['state'] == 'planned' and c['plan_hash'] != old
    assert c['plan']['items'][0]['from_release_id'] == r3['id']
    assert _approve(world, c['id'], 'amy', old).status_code == 409
    assert _approve(world, c['id'], 'amy', c['plan_hash']).json['state'] == 'applying'


def test_scheduled_change_goes_stale_when_the_fleet_moves_before_its_window(world):
    world.start(change_control={'timezone': 'America/New_York', 'windows': WINDOWS,
                                'policy': {'release.deploy': {'approvals': 0, 'window': True}}}, auth=auth_on())
    r2, r3 = world.balena.release(world.fleet), world.balena.release(world.fleet)
    c = _post(world, [_deploy(r2['id'])]).json
    assert c['state'] == 'scheduled'
    world.balena.pin_fleet(world.fleet, r3)
    world.clock.now = datetime(2026, 11, 5, 7, 30, tzinfo=timezone.utc)
    world.tick()
    assert world.change(c['id'])['state'] == 'stale'
    assert world.fleet['should_be_running__release'] == r3['id'], 'nothing was applied'


def test_reject_and_cancel(world):
    world.start(change_control={'policy': {'env': {'approvals': 1}}}, auth=auth_on())
    a = _post(world, [_env_item()]).json
    r = world.client.post('/fleet/v1/changes/%s/reject' % a['id'], json={'reason': 'wrong value'},
                          headers=as_('amy'))
    assert r.json['state'] == 'rejected' and 'wrong value' in r.json['error']
    b = _post(world, [_env_item()]).json
    assert world.client.post('/fleet/v1/changes/%s/cancel' % b['id'], headers=as_('ci')).json['state'] == 'cancelled'
    assert world.client.post('/fleet/v1/changes/%s/cancel' % b['id'], headers=as_('ci')).status_code == 409
    listed = world.client.get('/fleet/v1/changes', query_string={'state': 'cancelled'}, headers=as_('ci')).json
    assert [x['id'] for x in listed['changes']] == [b['id']]


def test_signed_change_sets(world):
    key = Ed25519PrivateKey.generate()
    world.start(change_control={'require_signature': True,
                                'trusted_keys': [{'kid': 'ci-2026', 'ed25519': public_key_b64(key)}]})
    body = {'title': 'env', 'ticket': 'CHG9', 'items': [_env_item()], 'window': 'next'}
    r = world.client.post('/fleet/v1/changes', json=body)
    assert r.status_code == 403 and r.json['type'].endswith('signature-required')
    signed = dict(body, signature=sign_change(body, 'ci-2026', key))
    r = world.client.post('/fleet/v1/changes', json=signed)
    assert r.status_code == 201 and r.json['signature_kid'] == 'ci-2026'
    tampered = dict(signed, ticket='CHG10')
    r = world.client.post('/fleet/v1/changes', json=tampered)
    assert r.status_code == 403 and r.json['type'].endswith('signature-mismatch')
    other = Ed25519PrivateKey.generate()
    r = world.client.post('/fleet/v1/changes', json=dict(body, signature=sign_change(body, 'ci-2026', other)))
    assert r.status_code == 403 and r.json['type'].endswith('signature-invalid')


def test_idempotency_key_returns_the_same_change(world):
    world.start(change_control={'policy': {'env': {'approvals': 1}}})
    h = {'Idempotency-Key': 'mtl-abc'}
    a = world.client.post('/fleet/v1/changes', json={'items': [_env_item()]}, headers=h)
    b = world.client.post('/fleet/v1/changes', json={'items': [_env_item()]}, headers=h)
    assert a.status_code == 201 and b.status_code == 200 and a.json['id'] == b.json['id']


def test_gated_mode_refuses_direct_writes_that_need_approval(world):
    world.start(change_control={'mode': 'gated', 'policy': {'env': {'approvals': 1}, 'release': {'approvals': 1},
                                                            'op.device': {'approvals': 0}}})
    r = world.client.patch('/fleet/v1/fleets/%s/env' % FLEET_SEG, json={'set': {'A': '1'}})
    assert r.status_code == 409 and r.json['type'].endswith('change-control-required')
    r = world.client.put('/fleet/v1/fleets/%s/target-release' % FLEET_SEG, json={'release': world.r1['id']})
    assert r.status_code == 409
    r = world.client.post('/fleet/v1/fleets/%s/releases' % FLEET_SEG,
                          json={'git': {'repo': 'balena-fleets', 'ref': 'v2.2.0'}})
    assert r.status_code == 409
    d = world.devices[0]
    r = world.client.post('/fleet/v1/devices/%s/operations' % d['uuid'], json={'type': 'reboot'})
    assert r.status_code == 202
    assert world.client.get('/fleet/v1/fleets/%s/env' % FLEET_SEG).status_code == 200, 'reads are never gated'


def test_allowed_callers(world):
    world.start(change_control={'allowed_callers': ['machine']})
    r = world.client.post('/fleet/v1/changes', json={'items': [_env_item()]})
    assert r.status_code == 403 and r.json['type'].endswith('caller-not-allowed')


def test_validation_errors_point_at_the_item(world):
    world.start()
    r = world.client.post('/fleet/v1/changes', json={'items': [{'op': 'nope', 'fleet': FLEET}]})
    assert r.status_code == 422 and r.json['errors'][0]['pointer'] == '/items/0/op'
    r = world.client.post('/fleet/v1/changes', json={'items': [{'op': 'env.patch', 'fleet': 'admin/none',
                                                                'set': {'A': '1'}}]})
    assert r.status_code == 422 and r.json['errors'][0]['pointer'] == '/items/0/fleet'
    r = world.client.post('/fleet/v1/changes', json={'items': [_env_item()], 'window': 'tomorrow-ish'})
    assert r.status_code == 422 and r.json['errors'][0]['pointer'] == '/window'
