"""Shallow checkout of {repo, ref, path} for a release build.

`repo` is a bare name resolved under git.repo_base (allow-listed by git.allowed_repos), or a
full URL when git.allow_urls is on. An https token is passed with GIT_CONFIG_* env vars as an
Authorization header, so it never appears in argv, the URL or the build log.
"""
import base64
import os
import re
import subprocess
from typing import Mapping, Optional

_NAME = re.compile(r'^[A-Za-z0-9._-]{1,100}$')
_REF = re.compile(r'^[A-Za-z0-9._/+-]{1,200}$')
_SHA = re.compile(r'^[0-9a-f]{7,40}$')


class GitError(Exception):
    def __init__(self, message: str, status: int = 422, pointer: str = '/git'):
        super().__init__(message)
        self.message, self.status, self.pointer = message, status, pointer


def validate(git: Mapping) -> dict:
    if not isinstance(git, Mapping):
        raise GitError('git must be {repo, ref, path}')
    repo, ref, path = git.get('repo'), git.get('ref'), git.get('path') or ''
    if not isinstance(repo, str) or not repo:
        raise GitError('git.repo is required', pointer='/git/repo')
    if not isinstance(ref, str) or not _REF.match(ref) or '..' in ref or ref.startswith('-'):
        raise GitError('git.ref must be a branch, tag or commit', pointer='/git/ref')
    if not isinstance(path, str) or path.startswith('/') or '..' in path.split('/'):
        raise GitError('git.path must be a relative path inside the repo', pointer='/git/path')
    return {'repo': repo, 'ref': ref, 'path': path.strip('/')}


class GitSource:
    def __init__(self, repo_base: str, allowed_repos=(), allow_urls: bool = False, token: str = '',
                 ssh_key_file: str = '', timeout_s: int = 300, git: str = 'git'):
        self.repo_base = repo_base if repo_base.endswith(('/', ':')) else repo_base + '/'
        self.allowed = set(allowed_repos or ())
        self.allow_urls = allow_urls
        self._token = token
        self.ssh_key_file = ssh_key_file
        self.timeout_s = timeout_s
        self.git = git

    def resolve(self, repo: str) -> str:
        if '://' in repo or repo.startswith('git@') or repo.startswith('/'):
            if not self.allow_urls:
                raise GitError('full repository URLs are not allowed here; name a repo under %s' % self.repo_base,
                               pointer='/git/repo')
            return repo
        name = repo[:-4] if repo.endswith('.git') else repo
        if not _NAME.match(name):
            raise GitError('git.repo %r is not a repository name' % repo, pointer='/git/repo')
        if self.allowed and name not in self.allowed:
            raise GitError('git.repo %r is not in git.allowed_repos' % name, status=403, pointer='/git/repo')
        return self.repo_base + name + '.git' if not self.repo_base.startswith('/') else self.repo_base + name

    def _env(self, url: str) -> dict:
        env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': os.environ.get('HOME', '/tmp'),
               'GIT_TERMINAL_PROMPT': '0'}
        if self._token and url.startswith('https://'):
            basic = base64.b64encode(('x-access-token:%s' % self._token).encode()).decode()
            env.update({'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'http.extraHeader',
                        'GIT_CONFIG_VALUE_0': 'Authorization: Basic %s' % basic})
        if self.ssh_key_file:
            env['GIT_SSH_COMMAND'] = 'ssh -i %s -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new' \
                % self.ssh_key_file
        return env

    def _run(self, args, cwd, env) -> str:
        try:
            p = subprocess.run([self.git] + args, cwd=cwd, env=env, capture_output=True, text=True,
                               timeout=self.timeout_s)
        except subprocess.TimeoutExpired:
            raise GitError('git %s timed out' % args[0], status=502)
        if p.returncode != 0:
            msg = (p.stderr or p.stdout or '').strip().splitlines()
            raise GitError('git %s failed: %s' % (args[0], msg[-1] if msg else 'exit %d' % p.returncode),
                           status=502)
        return p.stdout

    def checkout(self, git: Mapping, dest: str) -> str:
        """Check out git.ref into dest; returns the commit sha."""
        url = self.resolve(git['repo'])
        env = self._env(url)
        ref = git['ref']
        try:
            self._run(['clone', '--quiet', '--depth', '1', '--branch', ref, '--', url, dest], None, env)
        except GitError:
            if not _SHA.match(ref):
                raise
            # A commit sha: fetch just that commit.
            os.makedirs(dest, exist_ok=True)
            self._run(['init', '--quiet'], dest, env)
            self._run(['fetch', '--quiet', '--depth', '1', '--', url, ref], dest, env)
            self._run(['checkout', '--quiet', 'FETCH_HEAD'], dest, env)
        return self._run(['rev-parse', 'HEAD'], dest, env).strip()

    def source_dir(self, checkout: str, path: Optional[str]) -> str:
        src = os.path.realpath(os.path.join(checkout, path or ''))
        if not src.startswith(os.path.realpath(checkout)) or not os.path.isdir(src):
            raise GitError('git.path %r is not a directory in the repo' % path, pointer='/git/path')
        return src
