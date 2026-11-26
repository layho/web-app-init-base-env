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
import stat
import tarfile
import time
import threading
from tempfile import TemporaryDirectory
from urllib.request import Request, build_opener, HTTPRedirectHandler
import uuid

ROOT = Path.home() / '.local/share/macmini-pr-tests'
LOCK_HANDOFF_PROTOCOL = 1  # Supported by trusted web-side manual PR runner
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
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            if attempt == 2:
                raise
            # A timeout is a transport failure too; keep the exact ref and
            # the same bounded retry budget. Log only the exception class.
            print('Merge ref fetch unavailable; bounded retry', attempt + 1,
                  type(exc).__name__, file=log, flush=True)
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


def test_candidate(repo, pr, log, progress=lambda phase: None):
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
                progress(phase)
                run(['git', 'init', '--bare', str(gitdir)], log)
                env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GIT_SSH_COMMAND': 'ssh -o BatchMode=yes'}
                fetch_merge_ref(gitdir, repo, pr["number"], log, env)
                parents = subprocess.check_output(['git', '-C', str(gitdir), 'show', '-s', '--format=%H %P', 'FETCH_HEAD'], text=True, timeout=30)
                merge = verify_parents(parents, head, base)
                if merge != pr['merge_commit_sha']:
                    raise RuntimeError('Candidate SHA changed before testing')
                phase = 'context'
                progress(phase)
                make_context(gitdir, merge, context)
                phase = 'dependencies'
                progress(phase)
                with context.open('rb') as source:
                    run(docker + ['build', '--file', '.macci.Dockerfile', '--tag', image, '-'], log, timeout=1800, stdin=source, env=docker_env)
                phase = 'test'
                progress(phase)
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

REPORT_MARKER = "[MACCI_REPORT] "
REPORT_PHASES = {"setup-failed","fetch-failed","context-failed","dependencies-failed",
                 "pytest-failed","compile-failed","frontend-failed","test-failed","ref-changed","passed"}


def failure_summary(raw, phase):
    """Return only bounded, grammar-checked test identifiers / compiler codes.
    Never include arbitrary error messages, environment or raw log lines.
    Candidate-controlled output is diagnostic, not an authorization receipt.
    """
    result = []
    for line in raw.splitlines():
        if len(line) > 512:
            continue
        if phase == "pytest-failed":
            match = re.fullmatch(r"FAILED (tests/[A-Za-z0-9_/]{1,120}\.py)::([A-Za-z_][A-Za-z0-9_]{0,79})(?:::(?:([A-Za-z_][A-Za-z0-9_]{0,79})))?(?:\[[^\r\n]*\])?(?: - .*)?",line)
            if match:
                row = {"file":match[1],"test":match[2]+("::"+match[3] if match[3] else ""),"category":"pytest_failure"}
            else:
                continue
        elif phase == "frontend-failed":
            match = re.fullmatch(r"(?:frontend/)?(src/[A-Za-z0-9_/-]{1,120}\.tsx?)\((\d{1,6}),(\d{1,6})\): error (TS\d{4}):.*",line)
            if not match:
                continue
            row = {"file":"frontend/"+match[1],"line":int(match[2]),"column":int(match[3]),"category":match[4]}
        else:
            continue
        if row not in result:
            result.append(row)
        if len(result) >= 12:
            break
    return result


def save_report(report):
    from tempfile import NamedTemporaryFile
    folder = ROOT / "report-outbox"
    folder.mkdir(mode=0o700,exist_ok=True)
    info=folder.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:
        raise ValueError("Unsafe report outbox")
    path=folder/(report["run_id"]+".json")
    with NamedTemporaryFile(mode="w",dir=folder,delete=False,encoding="utf-8") as stream:
        temp=Path(stream.name)
        try:
            json.dump(report,stream,ensure_ascii=True,separators=(",",":"))
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
            os.replace(temp,path)
        finally:
            temp.unlink(missing_ok=True)
    return path


def flush_reports(token):
    folder=ROOT/"report-outbox"
    if not folder.exists():
        return
    info=folder.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:
        raise ValueError("Unsafe report outbox")
    for path in sorted(folder.glob("*.json"))[:20]:
        try:
            fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
            with os.fdopen(fd,"r",encoding="utf-8") as stream:
                info=os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077 or info.st_size>12000:
                    raise ValueError("Unsafe report")
                report=json.load(stream)
            if report.get("repository") not in REPOS or type(report.get("pr")) is not int or report["pr"]<=0 or not re.fullmatch(r"[0-9a-f]{32}",report.get("run_id","")) or path.stem!=report["run_id"] or any(not SHA.fullmatch(str(report.get(k,""))) for k in ("head","base","merge")) or report.get("phase") not in REPORT_PHASES:
                raise ValueError("Invalid report")
            endpoint=f"/repos/{report['repository']}/issues/{report['pr']}/comments"
            # Compare the exact serialized report before retrying a lost POST.
            body=REPORT_MARKER+json.dumps(report,sort_keys=True,separators=(",",":"))
            found=False
            for page in range(1,11):
                comments=api(token,endpoint+f"?per_page=100&page={page}")
                if any(c.get("body")==body and c.get("author_association")=="OWNER" for c in comments):
                    found=True
                    break
                if len(comments)<100:
                    break
            else:
                raise ValueError("Report dedup pagination limit")
            if not found:
                api(token,endpoint,{"body":body})
            path.unlink()
        except (OSError,ValueError,KeyError,TypeError):
            print("MacCI report delivery pending; will retry",flush=True)


PROGRESS_CONTEXT = "macmini/progress-v1"
PROGRESS_PHASES = {"setup","fetch","context","dependencies","test","cleanup"}


class Progress:
    """Independent advisory status. Never replace the authoritative test context."""
    def __init__(self, token, repo, head, base, merge):
        self.token, self.repo, self.head = token, repo, head
        self.base, self.merge = base, merge
        self.phase = "setup"
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.heartbeat, daemon=True)

    def send(self):
        try:
            api(self.token,f"/repos/{self.repo}/statuses/{self.head}",{
                "state":"pending","context":PROGRESS_CONTEXT,
                "description":description(self.base,self.merge,self.phase)})
        except (OSError,ValueError,TypeError):
            print("MacCI advisory progress unavailable",flush=True)

    def update(self, phase):
        if phase in PROGRESS_PHASES:
            self.phase=phase
            self.send()

    def heartbeat(self):
        while not self.stop.wait(30):
            self.send()

    def __enter__(self):
        self.send()
        self.thread.start()
        return self

    def __exit__(self,*args):
        self.stop.set()
        self.thread.join(timeout=35)


def finish_progress(token, repo, head, base, merge, state):
    """Close advisory context only after the authoritative receipt is written."""
    if state not in {"success", "failure", "error"}:
        return
    try:
        api(token, f"/repos/{repo}/statuses/{head}", {
            "state": state, "context": PROGRESS_CONTEXT,
            "description": description(base, merge, "finished")})
    except (OSError, ValueError, TypeError):
        print("MacCI advisory terminal delivery pending; retry next tick", flush=True)


def reconcile_progress(token, repo, head, base, merge, statuses):
    """Repair stranded progress without executing or inventing a test result."""
    receipt = next((s for s in statuses if s.get("context") == CONTEXT), None)
    advisory = next((s for s in statuses if s.get("context") == PROGRESS_CONTEXT), None)
    if not receipt or not advisory:
        return
    if any((s.get("creator") or {}).get("login") != "layho" for s in (receipt, advisory)):
        return
    state = receipt.get("state")
    valid_details = {"passed"} if state == "success" else REPORT_PHASES - {"passed", "ref-changed"}
    if state not in {"success", "failure", "error"} or not any(
            receipt.get("description") == description(base, merge, detail) for detail in valid_details):
        return
    if (advisory.get("state") == "pending" and any(
            advisory.get("description") == description(base, merge, phase) for phase in PROGRESS_PHASES)):
        finish_progress(token, repo, head, base, merge, state)


def status(token, repo, head, base, merge, state, detail=None):
    api(token, f'/repos/{repo}/statuses/{head}', {'state': state, 'context': CONTEXT,
        'description': description(base, merge, detail or state)})


@contextmanager
def lock():
    with (ROOT / 'worker.lock').open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


@contextmanager
def inherited_lock(fd):
    """Adopt the web handler's already locked worker file without a release gap."""
    if not isinstance(fd, int) or fd < 3:
        raise ValueError('Invalid inherited worker lock')
    info = os.fstat(fd)
    expected = (ROOT / 'worker.lock').lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o022
            or (info.st_dev, info.st_ino) != (expected.st_dev, expected.st_ino)):
        raise ValueError('Unsafe inherited worker lock')
    with os.fdopen(fd, 'a', closefd=True):
        yield


def shared_token_path():
    """Explicit opt-in: a private, same-user host credential; never use a symlink."""
    root = Path.home() / ".local/share/macmini-github"
    try:
        info = root.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Unsafe shared GitHub credential directory")
    path = root / "github-token"
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    return path


def token_file():
    return shared_token_path() or ROOT / 'github-token'


DEPLOY_CONTEXT = "macmini/deployment-v1"
DEPLOY_TERMINAL = {"update_ok","update_test_build_failed","update_restart_failed",
                   "update_dependencies_failed","update_schema_failed","update_preflight_failed",
                   "install_failed"}


def deployment_events(raw):
    """Recover terminal attempts from the existing durable operations journal.
    No raw details, credentials, or log paths are transmitted."""
    attempt = None
    reports = []
    for line in raw.splitlines():
        cols=line.split("\t")
        if len(cols)<3:
            continue
        stamp,code,sha=cols[:3]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z",stamp) or not SHA.fullmatch(sha):
            continue
        if code=="update_started":
            attempt={"started":stamp,"previous":sha,"candidate":None,"rollback":"unknown","failures":[]}
        elif attempt is not None:
            if code=="update_candidate":
                attempt["candidate"]=sha
            elif code=="host_test_case_failed" and sha==attempt["candidate"] and len(cols)==4:
                identity=cols[3]
                if re.fullmatch(r"tests/[A-Za-z0-9_/]{1,120}\.py::[A-Za-z_][A-Za-z0-9_]{0,79}(?:::[A-Za-z_][A-Za-z0-9_]{0,79})?",identity) and identity not in attempt["failures"] and len(attempt["failures"])<12:
                    attempt["failures"].append(identity)
            elif code=="rollback_ok" and sha==attempt["previous"]:
                attempt["rollback"]="ok"
            elif code=="rollback_failed" and sha==attempt["candidate"]:
                attempt["rollback"]="failed"
            elif code in DEPLOY_TERMINAL and sha==attempt["candidate"]:
                reports.append({**attempt,"finished":stamp,"code":code,"detail":cols[3] if len(cols)>3 and cols[3] in {"host_test_failed","frontend_build_failed"} else ""})
                attempt=None
            elif code in {"update_no_change","update_held"}:
                attempt=None
    return reports


def report_base_deployments(token):
    # Scope initial rollout to the known foundation preview checkout.
    # A future registry is needed for downstream installations.
    repo="layho/web-app-init-base-env"
    checkout=Path.home()/"web-app-init-base-env-preview"
    path=checkout/"logs/operations.tsv"
    try:
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,"rb") as stream:
            info=os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o022:
                return
            stream.seek(max(0,info.st_size-1048576))
            raw=stream.read(1048576).decode("utf-8",errors="replace")
        for row in deployment_events(raw)[-12:]:
            sha=row["candidate"]
            # Status is scoped to the immutable candidate. No merge authority.
            state="success" if row["code"]=="update_ok" else "failure"
            detail=(row["detail"] or row["code"])+" rollback="+row["rollback"]+" at="+row["finished"]
            statuses=api(token,f"/repos/{repo}/commits/{sha}/statuses?per_page=100")
            existing=next((s for s in statuses if s.get("context")==DEPLOY_CONTEXT and
                           (s.get("creator") or {}).get("login")=="layho"),None)
            if not existing or (existing.get("description")!=detail and
                str(existing.get("description","")).rsplit(" at=",1)[-1]<=row["finished"]):
                api(token,f"/repos/{repo}/statuses/{sha}",{"state":state,"context":DEPLOY_CONTEXT,"description":detail})
            # Stable event identity: a lost acknowledgement cannot create another issue.
            if state=="failure":
                title=f"[BASE-DEPLOY-FAILURE {sha}]"
                found=None
                for page in range(1,11):
                    issues=api(token,f"/repos/{repo}/issues?state=all&per_page=100&page={page}")
                    found=next((i for i in issues if i.get("title")==title and "pull_request" not in i and (i.get("user") or {}).get("login")=="layho"),None)
                    if found or len(issues)<100:
                        break
                else:
                    raise ValueError("Deployment failure issue pagination exhausted")
                if not found:
                    body="[BASE_DEPLOY_FAILURE] "+json.dumps({"schema":1,"candidate_sha":sha,**row},sort_keys=True,separators=(",",":"))
                    api(token,f"/repos/{repo}/issues",{"title":title,"body":body})
    except (OSError,ValueError,KeyError,TypeError):
        print("Deployment journal delivery pending; will retry",flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--configure', action='store_true')
    parser.add_argument('--repo', choices=REPOS)
    parser.add_argument('--pr', type=int)
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--lock-fd', type=int, default=None, help=argparse.SUPPRESS)
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
    if args.lock_fd is not None and (args.configure or args.pr is None or not args.force):
        parser.error('--lock-fd requires an explicit forced PR test')
    path = token_file()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "r", encoding="ascii") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 512:
            raise SystemExit("Unsafe token file permissions")
        token = stream.read().strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,300}", token):
        raise SystemExit("Invalid credential format")
    docker_command()
    subprocess.run(['docker', 'info', '--format', '{{.ServerVersion}}'], check=True, timeout=30)
    try:
        with (lock() if args.lock_fd is None else inherited_lock(args.lock_fd)):
            report_base_deployments(token)
            flush_reports(token)
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
                        reconcile_progress(token, repo, head, base, expected_merge, statuses)
                        continue
                    print(f'Testing {repo} PR #{pr["number"]}: {head} against {base}', flush=True)
                    status(token, repo, head, base, None, 'pending')
                    logfile = ROOT / f'{repo.replace("/", "--")}-{pr["number"]}-{head[:12]}-{base[:12]}.log'
                    run_id=uuid.uuid4().hex
                    started=time.time()
                    with logfile.open('a', encoding='utf-8') as log:
                        offset=log.tell()
                        with Progress(token,repo,head,base,expected_merge) as progress:
                            state, merge, detail = test_candidate(repo, pr, log, progress.update)
                    # Restrict extraction to this attempt and a bounded tail.
                    with logfile.open('rb') as stream:
                        stream.seek(max(offset,logfile.stat().st_size-262144))
                        diagnostic=failure_summary(stream.read(262144).decode("utf-8",errors="replace"),detail)
                    try:
                        latest = api(token, f'/repos/{repo}/pulls/{pr["number"]}')
                        unchanged = current_candidate(token, repo, latest) == candidate
                    except (OSError, ValueError, KeyError, TypeError):
                        unchanged = False
                    if not unchanged or (state == 'success' and merge != expected_merge):
                        state, detail = 'error', 'ref-changed'
                    status(token, repo, head, base, expected_merge, state, detail)
                    # The heartbeat thread is stopped before the terminal status.
                    finish_progress(token, repo, head, base, expected_merge, state)
                    if state != "success":
                        report={"schema":1,"repository":repo,"pr":pr["number"],"head":head,"base":base,"merge":expected_merge,
                                "run_id":run_id,"state":state,"phase":detail,"started_at":int(started),
                                "finished_at":int(time.time()),"failures":diagnostic if detail!="ref-changed" else []}
                        try:
                            save_report(report)
                            flush_reports(token)
                        except (OSError,ValueError,TypeError):
                            print("MacCI report unavailable; local test log retained",flush=True)
                    print(f'{state}: {logfile}', flush=True)
                    return 0 if state == 'success' else 1
            print('No eligible untested PRs (already tested, closed, or outside the trusted project profile)', flush=True)
    except BlockingIOError:
        print('Another test is running; skip this tick', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
