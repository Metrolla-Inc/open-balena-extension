"""Release builds through this repo's remote builder (components/builder).

HttpBuilder speaks the protocol `balena push` uses (balena-cli src/utils/remote-build.ts):
  POST <builder>/v3/build?slug=<fleet>&dockerfilePath=&emulated=false&nocache=false&headless=false&isdraft=false
  Authorization: Bearer <balena API key>, Content-Encoding: gzip, body = gzipped tar of the source
The response streams a JSON array of messages: {message, replace?, isError?} build output, and
{type: "metadata", resource: "buildLogId", value: "<release id>"} naming the release.

CliBuilder shells out to `balena push <fleet> --source <dir>` with BALENARC_DATA_DIRECTORY in the
service's data dir. It is the fallback if the builder's HTTP contract changes: it costs a CLI
in the image (which expires; see components/builder) and only gets text output to parse.
"""
import fnmatch
import gzip
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass, field
from typing import Callable, List, Optional
from urllib.parse import urlencode

import requests

_COMMIT_RE = re.compile(r'Release:\s*([0-9a-f]{7,40})', re.I)
_ID_RE = re.compile(r'\(id:\s*(\d+)\)', re.I)
_ANSI = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')

LineSink = Callable[[str], None]


@dataclass
class BuildResult:
    ok: bool
    release_id: Optional[int] = None
    commit: Optional[str] = None
    error: Optional[str] = None
    lines: List[str] = field(default_factory=list)


def _read_dockerignore(src: str) -> List[str]:
    path = os.path.join(src, '.dockerignore')
    if not os.path.isfile(path):
        return []
    with open(path) as fh:
        return [ln.strip() for ln in fh if ln.strip() and not ln.lstrip().startswith('#')]


def _ignored(rel: str, patterns: List[str]) -> bool:
    out = False
    for p in patterns:
        neg = p.startswith('!')
        pat = p[1:] if neg else p
        pat = pat.strip('/')
        if pat.startswith('./'):
            pat = pat[2:]
        if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(rel, pat + '/*') or \
                (pat.startswith('**/') and fnmatch.fnmatch(rel, pat[3:])):
            out = not neg
    return out


def tar_source(src: str, out) -> int:
    """Write a gzipped tar of src (minus .git and .dockerignore matches) to the file `out`."""
    patterns = _read_dockerignore(src)
    count = 0
    with gzip.GzipFile(fileobj=out, mode='wb', compresslevel=6, mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode='w') as tar:
            for root, dirs, files in os.walk(src):
                rel_root = os.path.relpath(root, src)
                rel_root = '' if rel_root == '.' else rel_root
                dirs[:] = sorted(d for d in dirs if d != '.git'
                                 and not _ignored(os.path.join(rel_root, d) if rel_root else d, patterns))
                for f in sorted(files):
                    rel = os.path.join(rel_root, f) if rel_root else f
                    if f == '.dockerignore' or not _ignored(rel, patterns):
                        tar.add(os.path.join(root, f), arcname=rel, recursive=False)
                        count += 1
    return count


def iter_json_array(chunks):
    """Objects from a streamed JSON array (or concatenated / newline-delimited objects)."""
    dec = json.JSONDecoder()
    buf = ''
    for chunk in chunks:
        if not chunk:
            continue
        buf += chunk.decode('utf-8', 'replace') if isinstance(chunk, bytes) else chunk
        while True:
            stripped = buf.lstrip(' \t\r\n[,')
            if not stripped or stripped.startswith(']'):
                buf = ''
                break
            try:
                obj, end = dec.raw_decode(stripped)
            except ValueError:
                buf = stripped
                break
            yield obj
            buf = stripped[end:]


class HttpBuilder:
    def __init__(self, url: str, api_key: str, verify=True, timeout_s: int = 3600,
                 session: Optional[requests.Session] = None):
        self.url = url.rstrip('/')
        self._key = api_key
        self.verify = verify
        self.timeout_s = timeout_s
        self.http = session or requests.Session()

    def build(self, slug: str, source_dir: str, on_line: LineSink = lambda s: None) -> BuildResult:
        q = urlencode({'slug': slug, 'dockerfilePath': '', 'emulated': 'false', 'nocache': 'false',
                       'headless': 'false', 'isdraft': 'false'})
        res = BuildResult(ok=False)
        with tempfile.TemporaryFile() as tf:
            tar_source(source_dir, tf)
            tf.seek(0)
            try:
                r = self.http.post('%s/v3/build?%s' % (self.url, q), data=tf, stream=True, verify=self.verify,
                                   timeout=(30, self.timeout_s),
                                   headers={'Authorization': 'Bearer ' + self._key, 'Content-Encoding': 'gzip',
                                            'Content-Type': 'application/x-tar'})
            except requests.RequestException as e:
                res.error = 'remote builder unreachable: %s' % type(e).__name__
                return res
            with r:
                if r.status_code >= 400:
                    res.error = 'remote builder answered HTTP %d: %s' % (r.status_code, (r.text or '')[:300])
                    return res
                had_error = False
                try:
                    for msg in iter_json_array(r.iter_content(chunk_size=None)):
                        if not isinstance(msg, dict):
                            continue
                        if msg.get('type') == 'metadata':
                            if msg.get('resource') == 'buildLogId' and str(msg.get('value') or '').isdigit():
                                res.release_id = int(msg['value'])
                            continue
                        text = _ANSI.sub('', str(msg.get('message') or '')).rstrip('\n')
                        if text and not msg.get('replace'):
                            res.lines.append(text)
                            on_line(text)
                            m = _COMMIT_RE.search(text)
                            if m:
                                res.commit = m.group(1)
                        if msg.get('isError'):
                            had_error = True
                except requests.RequestException as e:
                    res.error = 'build stream broke: %s' % type(e).__name__
                    return res
        if had_error:
            res.error = 'the remote builder reported an error (see the build log)'
            return res
        res.ok = True
        return res


class CliBuilder:
    def __init__(self, cli: str, data_dir: str, api_key: str, balena_url: str, timeout_s: int = 3600):
        self.cli = cli
        self.data_dir = data_dir
        self._key = api_key
        self.balena_url = balena_url
        self.timeout_s = timeout_s

    def _prepare(self):
        os.makedirs(self.data_dir, mode=0o700, exist_ok=True)
        token = os.path.join(self.data_dir, 'token')
        fd = os.open(token, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as fh:
            fh.write(self._key)

    def build(self, slug: str, source_dir: str, on_line: LineSink = lambda s: None) -> BuildResult:
        self._prepare()
        env = {'PATH': os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin'), 'HOME': self.data_dir,
               'BALENARC_DATA_DIRECTORY': self.data_dir, 'BALENARC_BALENA_URL': self.balena_url,
               'BALENARC_NO_ANALYTICS': '1', 'NODE_TLS_REJECT_UNAUTHORIZED': os.environ.get(
                   'NODE_TLS_REJECT_UNAUTHORIZED', '1')}
        if os.environ.get('NODE_EXTRA_CA_CERTS'):
            env['NODE_EXTRA_CA_CERTS'] = os.environ['NODE_EXTRA_CA_CERTS']
        res = BuildResult(ok=False)
        try:
            p = subprocess.Popen([self.cli, 'push', slug, '--source', source_dir], env=env, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True)
        except OSError as e:
            res.error = 'cannot run %s: %s' % (self.cli, e)
            return res
        try:
            for line in p.stdout:
                text = _ANSI.sub('', line).rstrip()
                if not text:
                    continue
                res.lines.append(text)
                on_line(text)
                m = _COMMIT_RE.search(text)
                if m:
                    res.commit = m.group(1)
                m = _ID_RE.search(text)
                if m:
                    res.release_id = int(m.group(1))
            code = p.wait(timeout=self.timeout_s)
        except subprocess.TimeoutExpired:
            p.kill()
            res.error = 'balena push timed out'
            return res
        if code != 0:
            res.error = 'balena push exited %d' % code
            return res
        res.ok = True
        return res


def which(cli: str) -> bool:
    return shutil.which(cli) is not None

