"""Small pieces: the builder stream parser, the CLI builder fallback, webhook signatures."""
import hashlib
import hmac
import json
import os
import stat
from datetime import datetime

from metrolla_auth import change_control as cc

from fleet_api.builder import CliBuilder, iter_json_array
from fleet_api.jobs import InlineJobs
from fleet_api.webhooks import Notifier


def test_builder_stream_parser_handles_split_chunks():
    text = '[{"message":"a"},{"type":"metadata","resource":"buildLogId","value":"12"},\n{"message":"b ]"}]'
    chunks = [text[i:i + 3].encode() for i in range(0, len(text), 3)]
    assert list(iter_json_array(chunks)) == [{'message': 'a'}, {'type': 'metadata', 'resource': 'buildLogId',
                                                                 'value': '12'}, {'message': 'b ]'}]
    assert list(iter_json_array([b'{"a":1}\n{"b":2}\n'])) == [{'a': 1}, {'b': 2}]


def test_cli_builder_uses_a_service_owned_data_dir(tmp_path):
    cli = tmp_path / 'balena'
    cli.write_text('#!/bin/sh\n'
                   'test "$1" = push || exit 9\n'
                   'test -f "$BALENARC_DATA_DIRECTORY/token" || exit 8\n'
                   'echo "[Info] Uploading source package to https://builder.$BALENARC_BALENA_URL"\n'
                   'echo "[Info] Release: 0123456789abcdef (id: 4242)"\n')
    cli.chmod(cli.stat().st_mode | stat.S_IEXEC)
    data = tmp_path / 'balenarc'
    lines = []
    res = CliBuilder(str(cli), str(data), 'the-key', 'example.local').build('admin/x', str(tmp_path), lines.append)
    assert res.ok and res.release_id == 4242 and res.commit == '0123456789abcdef'
    assert 'builder.example.local' in lines[0]
    assert (data / 'token').read_text() == 'the-key' and oct(os.stat(data / 'token').st_mode & 0o777) == '0o600'


class _Session:
    def __init__(self):
        self.posts = []

    def post(self, url, data, headers, timeout):
        self.posts.append((url, data, headers))

        class R:
            status_code = 200
        return R()


def test_webhook_events_filter_and_sign(tmp_path):
    secret = tmp_path / 'hook'
    secret.write_text('s3')
    sess = _Session()
    n = Notifier(lambda: [{'url': 'https://a/hook', 'events': ['fleet.change.applied'], 'secret_file': str(secret)},
                          {'url': 'https://b/hook'}], InlineJobs(), lambda: datetime(2026, 1, 1),
                 session=sess, sleep=lambda s: None)
    n.emit('fleet.change.applied', {'change_id': 'fchg_1', 'ticket': 'CHG1'})
    n.emit('fleet.change.planned', {'change_id': 'fchg_1', 'ticket': 'CHG1'})
    assert [p[0] for p in sess.posts] == ['https://a/hook', 'https://b/hook', 'https://b/hook']
    url, data, headers = sess.posts[0]
    assert json.loads(data)['ticket'] == 'CHG1' and headers['X-Metrolla-Event'] == 'fleet.change.applied'
    assert headers['X-Metrolla-Signature'] == 'sha256=' + hmac.new(b's3', data, hashlib.sha256).hexdigest()
    assert 'X-Metrolla-Signature' not in sess.posts[1][2]
    # The same wire format as EdgeAPI's change webhooks: canonical JSON, a delivery id per POST.
    assert data == cc.canonical_json(json.loads(data))
    assert len({p[2]['X-Metrolla-Delivery'] for p in sess.posts}) == 3


def test_change_control_webhooks_use_the_shared_signature():
    hooks = cc.parse_change_control({'webhooks': [
        {'url': 'https://itsm/hook', 'secret_env': 'ITSM_SECRET', 'events': ['fleet.change.*']},
        {'url': 'https://inline/hook', 'secret': 'inline'},
        {'url': 'https://plain/hook'}]}).webhooks
    sess = _Session()
    n = Notifier(lambda: hooks, InlineJobs(), lambda: datetime(2026, 1, 1), session=sess, sleep=lambda s: None,
                 environ={'ITSM_SECRET': 'from-env'})
    n.emit('fleet.change.applied', {'change_id': 'fchg_1'})
    n.emit('server.op.done', {})
    assert [p[0] for p in sess.posts] == ['https://itsm/hook', 'https://inline/hook', 'https://plain/hook',
                                          'https://inline/hook', 'https://plain/hook']
    (_, data, h1), (_, data2, h2), (_, _, h3) = sess.posts[:3]
    assert cc.verify_signature('from-env', data, h1[cc.SIGNATURE_HEADER])
    assert h2[cc.SIGNATURE_HEADER] == cc.signature('inline', data2)
    assert cc.SIGNATURE_HEADER not in h3


def test_example_configs_parse():
    import yaml
    from metrolla_auth import settings_from_document
    from fleet_api.settings import SettingsProvider
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    s = SettingsProvider(os.path.join(here, 'fleet-api.example.yaml'), environ={'DNS_TLD': 'lab.local'}).current()
    assert s['balena']['api_url'] == 'https://api.lab.local' and s.change_control.mode == 'open'
    with open(os.path.join(here, 'auth.example.yaml')) as fh:
        auth = settings_from_document(yaml.safe_load(fh), {})
    assert auth.auth.enabled is False and auth.auth.anonymous_role == 'site_admin'
