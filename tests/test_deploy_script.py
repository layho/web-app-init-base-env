"""Deployment control-flow regression without starting macOS services."""
from pathlib import Path
import subprocess

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "macmini" / "manage.sh"

def test_macmini_shell_syntax():
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

def test_schema_gate_is_retryable_and_real_failures_are_held():
    script = SCRIPT.read_text()
    assert 'if ! check_schema; then' in script
    assert 'rollback "$old" "$new" retryable' in script
    assert 'if test "${3:-hold}" = "hold"; then' in script
    assert 'if ! test_app || ! build; then' in script
    assert 'rollback "$old" "$new"; exit 1' in script
    assert 'git merge-base --is-ancestor "$old" "$new"' in script

def test_reinstall_preserves_previous_assets_until_healthy():
    script = SCRIPT.read_text()
    install_body = script.split("install() {", 1)[1].split("\nfeedback() {", 1)[0]
    assert "if test -d frontend/dist; then mv frontend/dist frontend/dist.previous; fi" in install_body
    assert "mv frontend/dist.next frontend/dist" in install_body
    assert "if ! agents || ! health" in install_body
    assert "if test -d frontend/dist.previous; then mv frontend/dist.previous frontend/dist; fi" in install_body
    assert install_body.index("mv frontend/dist.next frontend/dist") < install_body.index("if ! agents || ! health")
    assert install_body.index("if ! agents || ! health") < install_body.index("rm -rf frontend/dist.previous")
    assert 'launchctl bootstrap "gui/$(id -u)" "$AGENT_DIR/$label.plist" || return 1' in script


def test_update_stops_build_when_npm_ci_fails(tmp_path):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "package-lock.json").write_text("{}")
    script = SCRIPT.read_text()
    build_function = script[script.index("build() {"):script.index("health_once() {")]
    program = build_function + """
node() { return 0; }
npm() {
    if test "$1" = "--prefix"; then return 42; fi
    echo 'build attempted after npm ci failed'
    mkdir -p dist.next
    printf test > dist.next/index.html
}
set -e
if ! build; then exit 42; fi
"""
    result = subprocess.run(["bash", "-c", program], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 42, result.stdout + result.stderr
    assert "build attempted" not in result.stdout


def test_update_restores_git_and_assets_when_health_fails(tmp_path):
    program = r'''
set -euo pipefail
# This fixture simulates LaunchAgent restarts with the launchctl stub below.
unset APP_INIT_SYSTEM_SERVICE
git init -q -b main remote
git -C remote config user.name 'App tests'
git -C remote config user.email webapp-tests@example.invalid
git -C remote commit -qm previous --allow-empty
git clone -q remote local
git -C remote commit -qm candidate --allow-empty
cd local
old=$(git rev-parse HEAD)
new=$(git -C ../remote rev-parse HEAD)
mkdir -p frontend/dist logs
printf previous > frontend/dist/index.html

# Run the real updater and rollback with fake app gates and process supervision.
source ../functions.sh
load_env() { :; }
dependencies() { :; }
check_schema() { :; }
test_app() { :; }
build() {
    mkdir -p frontend/dist.next
    printf candidate > frontend/dist.next/index.html
}
launchctl() { printf '%s\n' "$(git rev-parse HEAD)" >> restarts; }
health_once() { return 1; }
health() {
    if test "$1" = "$old"; then
      test "$(git rev-parse HEAD)" = "$old" && test "$(cat frontend/dist/index.html)" = previous
      return
    fi
    test "$1" = "$new" || return 2
    test "$(git rev-parse HEAD)" = "$new" || return 2
    test "$(cat frontend/dist/index.html)" = candidate || return 2
    touch health-checked
    return 1
}
mkdir -p .venv/bin
printf '#!/bin/bash\nexit 0\n' > .venv/bin/python
chmod +x .venv/bin/python
ROOT=$PWD
LOCK=$ROOT/.webapp-deploy.lock
WEB=local-test
set +e
(set -e; update)
rc=$?
set -e
test "$rc" = 1
test -f health-checked
grep -q $'\trollback\t' logs/operations.tsv
grep -q "$(printf '\trollback_ok\t%s' "$old")" logs/operations.tsv
if grep -q $'\tupdate_ok\t' logs/operations.tsv; then exit 2; fi
test "$(git rev-parse HEAD)" = "$old"
test "$(cat frontend/dist/index.html)" = previous
test "$(cat .webapp-failed-sha)" = "$new"
test "$(cat restarts)" = "$(printf '%s\n%s' "$new" "$old")"
test ! -d frontend/dist.previous
test ! -d "$LOCK"
(set -e; update)
test "$(wc -l < restarts)" -eq 2
rm .webapp-failed-sha
mkdir frontend/dist.previous
printf recoverable > frontend/dist.previous/index.html
set +e
(set -e; update)
rc=$?
set -e
test "$rc" = 1
test "$(git rev-parse HEAD)" = "$old"
test "$(cat frontend/dist/index.html)" = previous
test "$(cat frontend/dist.previous/index.html)" = recoverable
test "$(wc -l < restarts)" -eq 2
test ! -d "$LOCK"
rm -r frontend/dist.previous
health() { test "$1" = "$new"; }
(set -e; update)
grep -q "$(printf '\tupdate_ok\t%s' "$new")" logs/operations.tsv
'''
    script = SCRIPT.read_text()
    functions = script[script.index("record_operation() {"):script.index('case "$1" in')]
    (tmp_path / "functions.sh").write_text(functions)
    result = subprocess.run(["bash", "-c", program], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_web_startup_checks_schema_and_assets_before_running_uvicorn(tmp_path):
    script = SCRIPT.read_text()
    serve_function = script[script.index("serve() {"):script.index("agents() {")]
    commands = serve_function + """
git() { printf 'test-sha\n'; }
record_operation() { :; }
load_env() { :; }
check_schema() { printf '%s\n' schema-checked; return 8; }
set -e
serve
"""
    blocked = subprocess.run(["bash", "-c", commands], cwd=tmp_path, capture_output=True, text=True)
    assert blocked.returncode == 8
    assert blocked.stdout.strip() == "schema-checked"

    missing = serve_function + """
git() { printf 'test-sha\n'; }
record_operation() { :; }
load_env() { :; }
check_schema() { :; }
log() { printf '%s\n' "$1"; }
set -e
serve
"""
    result = subprocess.run(["bash", "-c", missing], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 1
    assert result.stdout.strip() == "Missing built frontend"


def test_task_sync_runs_after_feedback_failure_and_without_token(tmp_path):
    script = SCRIPT.read_text()
    feedback_function = script[script.index("feedback() {"):script.index("restart_web() {")]
    # Run the actual function with mocked Python invocations, never GitHub.
    program = feedback_function + r'''
load_env() { :; }
git() { printf 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n'; }
record_operation() { :; }
log() { :; }
ROOT="$PWD"
mkdir -p .venv/bin
cat > .venv/bin/python <<'SH'
#!/bin/bash
case "$*" in
  *sync_feedback*) echo "feedback failed" >> calls; exit 7;;
  *sync_development_tasks*) echo "tasks synced" >> calls; exit 0;;
esac
SH
chmod +x .venv/bin/python
APP_GITHUB_FEEDBACK_TOKEN=dummy
set +e
feedback
rc=$?
set -e
test "$rc" = 7
grep -q "tasks synced" calls
: > calls
APP_GITHUB_FEEDBACK_TOKEN=
feedback
test ! -s calls
'''
    result = subprocess.run(["bash", "-c", program], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_system_restart_uses_authoritative_job_and_bounded_shutdown(tmp_path):
    script = SCRIPT.read_text()
    restart = script[script.index("restart_web() {"):script.index("rollback() {")]
    program = restart + r'''
set -euo pipefail
APP_INIT_SYSTEM_SERVICE=1
ROOT=$PWD
WEB=test.web
PORT=25936
mkdir logs
printf '99999\n' > logs/web.pid
log() { :; }
sleep() { :; }
launchctl() {
    test "$*" = "print system/test.web" || return 71
    case "$mode" in
        waiting) printf 'state = waiting\n';;
        replaced) if test -s signals; then printf 'pid = 43\n'; else printf 'pid = 42\n'; fi;;
        *) printf 'state = running\n\tpid = 42\n';;
    esac
}
ps() {
    test "$2" = 42 || return 72
    case "$4" in
        uid=) id -u;;
        lstart=) if test "$mode" = reused && test -s signals; then echo later; else echo original; fi;;
        command=) if test "$mode" = unrelated; then echo unrelated; else echo 'uvicorn app.main:app --host 127.0.0.1 --port 25936 --workers 1'; fi;;
        *) return 72;;
    esac
}
lsof() { if test "$mode" = other_cwd; then printf 'n/tmp/other-app\n'; else printf 'n%s\n' "$ROOT"; fi; }
kill() {
    case "$*" in
        "-TERM 42"|"-KILL 42") echo "$*" >> signals;;
        "-0 42") return 0;;
        *) return 73;;
    esac
}
mode=stuck
restart_web
test "$(cat signals)" = "$(printf '%s\n%s' '-TERM 42' '-KILL 42')"
: > signals
mode=replaced
restart_web
test "$(cat signals)" = "-TERM 42"
: > signals
mode=reused
restart_web
test "$(cat signals)" = "-TERM 42"
: > signals
mode=waiting
restart_web
test ! -s signals
mode=unrelated
if restart_web; then exit 74; fi
test ! -s signals
mode=other_cwd
if restart_web; then exit 75; fi
test ! -s signals
'''
    result = subprocess.run(["bash", "-c", program], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_rollback_reports_unhealthy_previous_service(tmp_path):
    script = SCRIPT.read_text()
    rollback = script[script.index("rollback() {"):script.index("update() {")]
    program = rollback + r'''
set -euo pipefail
WEB=test.web
mkdir -p .venv/bin logs frontend/dist frontend/dist.previous
printf previous > frontend/dist.previous/index.html
printf candidate > frontend/dist/index.html
printf '#!/bin/bash\nexit 0\n' > .venv/bin/python
chmod +x .venv/bin/python
git() { :; }
log() { echo "$*"; }
record_operation() { printf '%s %s\n' "$1" "$2" >> events; }
health_once() { return 1; }
restart_web() { return 1; }
health() { return 1; }
if rollback old bad; then exit 73; fi
test "$(cat .webapp-failed-sha)" = bad
test "$(cat frontend/dist/index.html)" = previous
grep -q '^rollback_failed bad$' events
! grep -q '^rollback_ok ' events
'''
    result = subprocess.run(["bash", "-c", program], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_update_reports_no_change_and_fetch_failure(tmp_path):
    script = SCRIPT.read_text()
    functions = script[script.index("record_operation() {"):script.index('case "$1" in')]
    program = functions + r'''
set -euo pipefail
ROOT=$PWD
LOCK=$ROOT/.webapp-deploy.lock
mkdir logs
sha=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
assert_main() { :; }
load_env() { :; }
git() {
    if test "$1" = rev-parse; then echo "$sha"; return 0; fi
    if test "$1" = fetch; then return "$fetch_rc"; fi
    return 99
}
fetch_rc=0
(update)
grep -q "$(printf '\tupdate_no_change\t%s' "$sha")" logs/operations.tsv
test ! -d "$LOCK"
fetch_rc=7
set +e
(set -e; update)
rc=$?
set -e
test "$rc" = 7
grep -q "$(printf '\tupdate_fetch_failed\t%s' "$sha")" logs/operations.tsv
test ! -d "$LOCK"
'''
    result = subprocess.run(["bash","-c",program],cwd=tmp_path,capture_output=True,text=True)
    assert result.returncode == 0, result.stdout + result.stderr

def test_read_only_update_check_does_not_deploy(monkeypatch):
    from app import operations
    from types import SimpleNamespace
    calls = []
    monkeypatch.setenv("APP_BASE_PREVIEW", "1")
    def fake_run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(stdout="b" * 40 + "\trefs/heads/preview\n")
    monkeypatch.setattr(operations.subprocess, "run", fake_run)
    result = operations.check_available_update("a" * 40)
    assert result["state"] == "available"
    assert result["remote_sha"] == "b" * 40
    assert len(calls) == 1
    assert "ls-remote" in calls[0]
    assert not any(word in calls[0] for word in ("fetch", "kickstart", "push"))


def test_read_only_update_check_unavailable_on_bad_git_response(monkeypatch):
    from app import operations
    from types import SimpleNamespace
    monkeypatch.delenv("APP_BASE_PREVIEW", raising=False)
    monkeypatch.setattr(operations.subprocess, "run", lambda *a, **k:
                        SimpleNamespace(stdout="bad-sha\trefs/heads/main\n"))
    assert operations.check_available_update("a" * 40)["state"] == "unavailable"
    assert operations.check_available_update("unknown")["state"] == "unavailable"


def test_fetch_error_classification_and_retry_without_secret_leak(tmp_path):
    script = SCRIPT.read_text()
    funcs = script[script.index("fetch_reason() {"):script.index("finish_update() {")]
    program = funcs + r'''
set -euo pipefail
UPDATE_SHA=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
UPDATE_DETAIL=
log() { echo "$*"; }
record_operation() { echo "$*"; }
sleep() { :; }
git() {
  if ! test -e attempted; then
    touch attempted
    echo 'Connection reset secret-example-token' >&2
    return 1
  fi
  return 0
}
fetch_with_retry
test "$(fetch_reason 'Permission denied (publickey)')" = authentication
test "$(fetch_reason 'Could not resolve host')" = dns
test "$(fetch_reason 'Could not resolve proxy')" = proxy
'''
    result = subprocess.run(["bash","-c",program],cwd=tmp_path,capture_output=True,text=True)
    assert result.returncode == 0, result.stderr
    assert "update_fetch_retry" in result.stdout
    assert "secret-example-token" not in result.stdout + result.stderr
