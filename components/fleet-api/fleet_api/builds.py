"""Release build jobs: clone, build through the remote builder, wait for the release, finalize.

Builds run one at a time on this host (concurrent `balena push` races on release and service
creation, AGENTS.md invariant 4). Finalizing here replaces the manual `balena release
finalize` after the builder's auto-finalize 404s.
"""
import logging
import shutil
import tempfile
import threading
import time
from typing import Callable, Optional

from .balena import BalenaClient, BalenaError
from .gitsrc import GitError, GitSource
from .models import Build, iso, new_id, utcnow

log = logging.getLogger('fleet_api')

MAX_LOG = 256 * 1024
DONE = ('success', 'failed')
_BUILD_LOCK = threading.Lock()


def build_to_dict(b: Build) -> dict:
    return {'kind': 'build', 'id': b.id, 'build_id': b.id, 'fleet': b.fleet, 'git': b.git, 'status': b.state,
            'release_id': b.release_id, 'commit': b.commit, 'source_commit': b.source_commit,
            'finalized': bool(b.finalized), 'error': b.error, 'created_by': b.created_by, 'change_id': b.change_id,
            'created_at': iso(b.created_at), 'started_at': iso(b.started_at), 'finished_at': iso(b.finished_at),
            'build_log_url': '/fleet/v1/builds/%s/log' % b.id}


class BuildRunner:
    def __init__(self, session_factory, balena: Callable[[], BalenaClient], builder: Callable, git: Callable[[], GitSource],
                 jobs, release_wait_s: int = 300, sleep=time.sleep, poll_s: float = 5.0):
        self.session_factory = session_factory
        self.balena = balena
        self.builder = builder
        self.git = git
        self.jobs = jobs
        self.release_wait_s = release_wait_s
        self.sleep = sleep
        self.poll_s = poll_s

    def start(self, fleet: dict, git: dict, actor: str, change_id: Optional[str] = None) -> dict:
        db = self.session_factory()
        try:
            b = Build(id=new_id('bld'), fleet=fleet['slug'], git=git, state='queued', created_by=actor,
                      change_id=change_id, log='')
            db.add(b)
            db.commit()
            out = build_to_dict(b)
        finally:
            db.close()
        self.jobs.submit(self.run, out['id'], fleet['id'])
        return out

    def get(self, build_id: str) -> Optional[Build]:
        db = self.session_factory()
        try:
            return db.get(Build, build_id)
        finally:
            db.close()

    def _update(self, build_id: str, **fields):
        db = self.session_factory()
        try:
            b = db.get(Build, build_id)
            for k, v in fields.items():
                setattr(b, k, v)
            db.commit()
        finally:
            db.close()

    def _append(self, build_id: str, lines):
        db = self.session_factory()
        try:
            b = db.get(Build, build_id)
            text = (b.log or '') + ''.join(ln + '\n' for ln in lines)
            if len(text) > MAX_LOG:
                text = '[… earlier output dropped …]\n' + text[-MAX_LOG:]
            b.log = text
            db.commit()
        finally:
            db.close()

    def run(self, build_id: str, app_id: int):
        with _BUILD_LOCK:
            b = self.get(build_id)
            if b is None or b.state != 'queued':
                return
            workdir = tempfile.mkdtemp(prefix='fleet-build-')
            buffered = []

            def sink(line):
                buffered.append(line)
                if len(buffered) >= 50:
                    self._append(build_id, buffered[:])
                    buffered.clear()

            try:
                self._update(build_id, state='cloning', started_at=utcnow())
                git = self.git()
                sha = git.checkout(b.git, workdir + '/src')
                src = git.source_dir(workdir + '/src', b.git.get('path'))
                self._update(build_id, state='building', source_commit=sha)
                self._append(build_id, ['[fleet-api] %s@%s (%s) path=%s' % (b.git['repo'], b.git['ref'], sha[:12],
                                                                             b.git.get('path') or '.')])
                result = self.builder().build(b.fleet, src, sink)
                if buffered:
                    self._append(build_id, buffered[:])
                    buffered.clear()
                balena = self.balena()
                release = self._wait_release(balena, app_id, result)
                if not result.ok or release is None or release.get('status') != 'success':
                    status = release.get('status') if release else 'unknown'
                    self._update(build_id, state='failed', release_id=release.get('id') if release else result.release_id,
                                 commit=release.get('commit') if release else result.commit, finished_at=utcnow(),
                                 error=result.error or 'release status is %s' % status)
                    return
                self._update(build_id, state='finalizing', release_id=release['id'], commit=release['commit'])
                if not release.get('is_final'):
                    balena.finalize_release(release['id'])
                    release = balena.release(release['id']) or release
                if not release.get('is_final'):
                    raise BalenaError(0, 'release %s did not become final' % release['id'])
                self._append(build_id, ['[fleet-api] release %s (%s) finalized' % (release['id'], release['commit'])])
                self._update(build_id, state='success', finalized=True, finished_at=utcnow())
            except (GitError, BalenaError) as e:
                self._update(build_id, state='failed', error=str(getattr(e, 'message', e)), finished_at=utcnow())
            except Exception as e:
                log.exception('build %s failed', build_id)
                self._update(build_id, state='failed', error='internal error: %s' % type(e).__name__,
                             finished_at=utcnow())
            finally:
                if buffered:
                    self._append(build_id, buffered)
                shutil.rmtree(workdir, ignore_errors=True)

    def _wait_release(self, balena: BalenaClient, app_id: int, result) -> Optional[dict]:
        """The release the build produced, once its status settles (images may still be pushing
        when the stream ends)."""
        rid = result.release_id
        if rid is None and result.commit:
            r = balena.find_release(app_id, result.commit)
            rid = r['id'] if r else None
        if rid is None:
            return None
        deadline = time.monotonic() + self.release_wait_s
        while True:
            release = balena.release(rid)
            if release is None or release.get('status') not in ('running', 'pending') \
                    or time.monotonic() >= deadline:
                return release
            self.sleep(self.poll_s)
