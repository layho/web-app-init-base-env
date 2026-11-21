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
    build_function = script[script.index("build() {"):script.index("health() {")]
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
health() {
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
