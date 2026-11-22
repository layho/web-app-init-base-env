"""Local trusted-repository PR test worker. No GitHub Actions or automatic merge."""
import argparse
from contextlib import contextmanager
import fcntl
from getpass import getpass
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import time
from tempfile import TemporaryDirectory
from urllib.request import Request, build_opener, HTTPRedirectHandler
import uuid

ROOT = Path.home() / '.local/share/macmini-pr-tests'
CONTEXT = 'macmini/isolated-tests-v1'
REPOS = {
    'layho/web-app-init-base-env': {'preview'},
    'layho/macmini-office-app-panel': {'main'},
    'layho/xiongzhan-ti-hunter': {'main'},
}
SHA = re.compile(r'[0-9a-f]{40}\Z')
DOCKERFILE = '''FROM node:22-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv git && rm -rf /var/lib/apt/lists/*
WORKDIR /work
COPY requirements.txt /work/requirements.txt
RUN python3 -m venv /opt/testenv && /opt/testenv/bin/python -m pip --isolated install --no-cache-dir --only-binary=:all: -r requirements.txt
COPY frontend/package.json frontend/package-lock.json /work/frontend/
RUN cd frontend && npm ci --ignore-scripts --no-audit --no-fund
COPY --chown=node:node . /work
RUN chown -R node:node /work
ENV PATH=/opt/testenv/bin:$PATH DATABASE_URL=sqlite:////tmp/macmini-ci.sqlite3 PYTHONDONTWRITEBYTECODE=1
USER node
CMD ["bash", "-c", "python -m pytest -q || exit 41; python -m compileall -q app || exit 42; npm --prefix frontend run build || exit 43"]
'''


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def api(token, path, data=None):
    req = Request('https://api.github.com' + path,
                  data=json.dumps(data).encode() if data is not None else None,
                  headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
                           'Content-Type': 'application/json', 'User-Agent': 'macmini-pr-tests'})
    with build_opener(NoRedirect()).open(req, timeout=30) as response:
        return json.load(response)


def eligible(repo, pr):
    return (repo in REPOS and pr.get('state') == 'open'
            and pr.get('author_association') in {'OWNER', 'MEMBER', 'COLLABORATOR'}
            and pr.get('base', {}).get('ref') in REPOS[repo]
            and (pr.get('head', {}).get('repo') or {}).get('full_name') == repo
            and all(SHA.fullmatch(str(pr.get(side, {}).get('sha', ''))) for side in ('head', 'base'))
            and type(pr.get('number')) is int and pr['number'] > 0)


def identity(pr):
    return pr['head']['sha'], pr['base']['sha']


def current_candidate(token, repo, pr):
    """Resolve the actual target branch and verify the ordered merge parents.
    PR base.sha can lag the branch; never use it as the test identity.
    """
    if not eligible(repo, pr):
        return None
    head = pr['head']['sha']
    branch = pr['base']['ref']
    base = api(token, f'/repos/{repo}/branches/{branch}')['commit']['sha']
    merge = pr.get('merge_commit_sha')
    if not all(SHA.fullmatch(str(x)) for x in (base, merge)):
        return None
    commit = api(token, f'/repos/{repo}/git/commits/{merge}')
    if [p['sha'] for p in commit['parents']] != [base, head]:
        return None
    return head, base, merge


def description(base, merge=None, result='pending'):
    return f'base={base} merge={merge or "unknown"} {result}'


def completed(statuses, base, merge=None):
    """Never let one temporary merge-ref fetch failure permanently block a PR.

    All other failures remain terminal; exact candidate and base match are
    mandatory. Three recorded fetch failures bound retry work per candidate.
    """
    prefix = f'base={base} merge={merge} ' if merge is not None else f'base={base} '
    matching = [s for s in statuses if s.get('context') == CONTEXT
                and s.get('description', '').startswith(prefix)]
    if not matching:
        return False
    latest = matching[0]
    if latest.get('state') not in {'success', 'failure', 'error'}:
        return False
    detail = latest.get('description', '')
    if detail.endswith(' ref-changed'):
        return False
    if detail.endswith(' fetch-failed') and latest.get('state') == 'failure':
        attempts = sum(s.get('state') == 'failure' and
                       s.get('description', '').endswith(' fetch-failed') for s in matching)
        return attempts >= 3
    return True


def verify_parents(output, head, base):
    fields = output.strip().split()
    if len(fields) != 3 or not SHA.fullmatch(fields[0]) or fields[1:] != [base, head]:
        raise RuntimeError('GitHub merge ref changed, unavailable, or does not match head/base')
    return fields[0]


def run(args, log, timeout=300, **kwargs):
    print('+', ' '.join(args), file=log, flush=True)
    return subprocess.run(args, check=True, timeout=timeout, stdout=log, stderr=log, **kwargs)


def fetch_merge_ref(gitdir, repo, number, log, env):
    """Retry only the exact GitHub merge ref; never substitute another SHA."""
    command = ['git', '-C', str(gitdir), 'fetch', '--no-tags', '--depth=2',
               f'git@github.com:{repo}.git', f'refs/pull/{number}/merge']
    for attempt in range(3):
        try:
            run(command, log, timeout=120, env=env)
            return
        except subprocess.CalledProcessError:
            if attempt == 2:
                raise
            print('Merge ref fetch unavailable; bounded retry', attempt + 1, file=log, flush=True)
            time.sleep(2 * (attempt + 1))


def make_context(gitdir, merge, path):
    with path.open('wb') as out:
        subprocess.run(['git', '-C', str(gitdir), 'archive', '--format=tar', merge], stdout=out, check=True, timeout=120)
    with tarfile.open(path, 'a') as archive:
        names = set(archive.getnames())
        required = {'requirements.txt', 'frontend/package.json', 'frontend/package-lock.json'}
        if not required <= names or not any(n.startswith('tests/test_') for n in names):
            raise RuntimeError('Missing locked dependencies or root pytest suite; configure project profile first')
        if {'.macci.Dockerfile', '.dockerignore', '.macci.Dockerfile.dockerignore'} & names:
            raise RuntimeError('Reserved CI build-context file exists; review configuration')
        data = DOCKERFILE.encode()
        member = tarfile.TarInfo('.macci.Dockerfile')
        member.size = len(data)
        archive.addfile(member, io.BytesIO(data))


def docker_command():
    # Resolve the configured local engine, then use a fresh CLI config so
    # cached registry credentials and automatic proxy build args are not exposed.
    endpoint = os.getenv('DOCKER_HOST') or subprocess.check_output(
        ['docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}'],
        text=True, timeout=30).strip()
    if not endpoint.startswith('unix:///'):
        raise RuntimeError('A local Unix-socket Docker engine is required')
    return endpoint


def test_candidate(repo, pr, log):
    head, base = identity(pr)
    job = 'macci-' + uuid.uuid4().hex
    image = job + ':test'
    merge = None
    phase = 'setup'
    docker = None
    docker_env = None
    with TemporaryDirectory(prefix='macci-docker-') as docker_config:
        try:
            Path(docker_config, 'config.json').write_text(json.dumps({'cliPluginsExtraDirs': ['/opt/homebrew/lib/docker/cli-plugins', '/usr/local/lib/docker/cli-plugins', '/Applications/Docker.app/Contents/Resources/cli-plugins']}))
            docker = ['docker', '--config', docker_config, '--host', docker_command()]
            docker_env = {'PATH': os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin'), 'HOME': docker_config}
            with TemporaryDirectory(prefix='macci-') as folder:
                gitdir, context = Path(folder) / 'repo.git', Path(folder) / 'context.tar'
                phase = 'fetch'
                run(['git', 'init', '--bare', str(gitdir)], log)
                env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GIT_SSH_COMMAND': 'ssh -o BatchMode=yes'}
                fetch_merge_ref(gitdir, repo, pr["number"], log, env)
                parents = subprocess.check_output(['git', '-C', str(gitdir), 'show', '-s', '--format=%H %P', 'FETCH_HEAD'], text=True, timeout=30)
                merge = verify_parents(parents, head, base)
                if merge != pr['merge_commit_sha']:
                    raise RuntimeError('Candidate SHA changed before testing')
                phase = 'context'
                make_context(gitdir, merge, context)
                phase = 'dependencies'
                with context.open('rb') as source:
                    run(docker + ['build', '--file', '.macci.Dockerfile', '--tag', image, '-'], log, timeout=1800, stdin=source, env=docker_env)
                phase = 'test'
                run(docker + ['run', '--name', job, '--network', 'none', '--cap-drop', 'ALL',
                     '--security-opt', 'no-new-privileges', '--pids-limit', '256', '--memory', '3g', '--cpus', '2', image], log, timeout=1200, env=docker_env)
            return 'success', merge, 'passed'
        except (OSError, subprocess.SubprocessError, RuntimeError, tarfile.TarError) as exc:
            print(type(exc).__name__, str(exc), file=log, flush=True)
            if phase == 'test' and isinstance(exc, subprocess.CalledProcessError):
                phase = {41: 'pytest', 42: 'compile', 43: 'frontend'}.get(exc.returncode, phase)
            return 'failure', merge, phase + '-failed'
        finally:
            if docker is not None:
                for cmd in (['rm', '-f', job], ['image', 'rm', image]):
                    try:
                        subprocess.run(docker + cmd, stdout=log, stderr=log, timeout=30, env=docker_env)
                    except (OSError, subprocess.SubprocessError):
                        pass

def status(token, repo, head, base, merge, state, detail=None):
    api(token, f'/repos/{repo}/statuses/{head}', {'state': state, 'context': CONTEXT,
        'description': description(base, merge, detail or state)})


@contextmanager
def lock():
    with (ROOT / 'worker.lock').open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def token_file():
    return ROOT / 'github-token'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--configure', action='store_true')
    parser.add_argument('--repo', choices=REPOS)
    parser.add_argument('--pr', type=int)
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.configure:
        token = getpass('GitHub token (PR read, commit statuses read/write): ').strip()
        if not re.fullmatch(r'[A-Za-z0-9_-]+', token):
            raise SystemExit('Invalid token format')
        api(token, '/repos/layho/web-app-init-base-env/pulls?per_page=1')
        fd = os.open(token_file(), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as out:
            out.write(token + '\n')
        print('Token saved privately. Never pass it to test containers.')
        return 0
    if args.pr is not None and (not args.repo or args.pr <= 0):
        parser.error('--pr requires --repo and a positive PR number')
    path = token_file()
    if path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise SystemExit('Unsafe token file permissions')
    token = path.read_text().strip()
    docker_command()
    subprocess.run(['docker', 'info', '--format', '{{.ServerVersion}}'], check=True, timeout=30)
    try:
        with lock():
            for repo in ([args.repo] if args.repo else REPOS):
                if args.pr:
                    prs = [api(token, f'/repos/{repo}/pulls/{args.pr}')]
                else:
                    prs = []
                    for page in range(1, 11):
                        batch = api(token, f'/repos/{repo}/pulls?state=open&sort=created&direction=asc&per_page=100&page={page}')
                        prs.extend(batch)
                        if len(batch) < 100:
                            break
                for pr in prs:
                    if not eligible(repo, pr):
                        continue
                    pr = api(token, f'/repos/{repo}/pulls/{pr["number"]}')
                    try:
                        candidate = current_candidate(token, repo, pr)
                    except (OSError, ValueError, KeyError, TypeError):
                        print(f'PR #{pr["number"]}: candidate unavailable; retry next tick', flush=True)
                        continue
                    if candidate is None:
                        print(f'PR #{pr["number"]}: waiting for current merge candidate', flush=True)
                        continue
                    head, base, expected_merge = candidate
                    pr = {**pr, 'base': {**pr['base'], 'sha': base}, 'merge_commit_sha': expected_merge}
                    statuses = api(token, f'/repos/{repo}/commits/{head}/statuses?per_page=100')
                    if completed(statuses, base, expected_merge) and not args.force:
                        continue
                    print(f'Testing {repo} PR #{pr["number"]}: {head} against {base}', flush=True)
                    status(token, repo, head, base, None, 'pending')
                    logfile = ROOT / f'{repo.replace("/", "--")}-{pr["number"]}-{head[:12]}-{base[:12]}.log'
                    with logfile.open('a', encoding='utf-8') as log:
                        state, merge, detail = test_candidate(repo, pr, log)
                    try:
                        latest = api(token, f'/repos/{repo}/pulls/{pr["number"]}')
                        unchanged = current_candidate(token, repo, latest) == candidate
                    except (OSError, ValueError, KeyError, TypeError):
                        unchanged = False
                    if not unchanged or (state == 'success' and merge != expected_merge):
                        state, detail = 'error', 'ref-changed'
                    status(token, repo, head, base, expected_merge, state, detail)
                    print(f'{state}: {logfile}', flush=True)
                    return 0 if state == 'success' else 1
            print('No eligible untested PRs (already tested, closed, or outside the trusted project profile)', flush=True)
    except BlockingIOError:
        print('Another test is running; skip this tick', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
