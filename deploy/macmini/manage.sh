#!/bin/bash
# Application deployment and maintenance on macOS.
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
ROOT="$(cd "$(dirname "$0")/../.." && pwd -P)"
cd "$ROOT"
LABEL_PREFIX="com.example.webapp"
PORT=18080
TRACK_BRANCH=main
EXPECTED_ORIGIN="https://app.example.com"
# Dedicated foundation preview configuration is local, never committed.
if test -f "$ROOT/.base-preview.env"; then
  test ! -L "$ROOT/.base-preview.env" && test -O "$ROOT/.base-preview.env" || exit 2
  test "$(stat -f '%Lp' "$ROOT/.base-preview.env")" = 600 || exit 2
  source "$ROOT/.base-preview.env"
  [[ "${BASE_PREVIEW_PORT:-}" =~ ^[0-9]{5}$ ]] &&
    test "$BASE_PREVIEW_PORT" -ge 18000 && test "$BASE_PREVIEW_PORT" -lt 29000 || exit 2
  LABEL_PREFIX="com.layho.basepreview"
  PORT="$BASE_PREVIEW_PORT"
  TRACK_BRANCH=preview
  export APP_BASE_PREVIEW=1
  EXPECTED_ORIGIN="https://base.layho.com"
  export VITE_BASE_PREVIEW=1
fi
AGENT_DIR="$HOME/Library/LaunchAgents"
if test -n "${APP_ACCEPTANCE_ID:-}"; then
  [[ "$APP_ACCEPTANCE_ID" =~ ^[a-zA-Z0-9_]+$ ]] || exit 2
  [[ "${APP_ACCEPTANCE_PORT:-}" =~ ^[0-9]{5}$ ]] &&
    test "$APP_ACCEPTANCE_PORT" -ge 18000 &&
    test "$APP_ACCEPTANCE_PORT" -le 18999 || exit 2
  LABEL_PREFIX="$LABEL_PREFIX.acceptance.$APP_ACCEPTANCE_ID"
  PORT="$APP_ACCEPTANCE_PORT"
  AGENT_DIR="$ROOT/.webapp-acceptance-agents"
fi
WEB="$LABEL_PREFIX.web"
UPDATER="$LABEL_PREFIX.updater"
FEEDBACK="$LABEL_PREFIX.feedback"
LOCK="$ROOT/.webapp-deploy.lock"
mkdir -p "$ROOT/logs"
record_operation() {
  local code="$1" sha="$2" stamp detail="${3:-}"
  [[ "$sha" =~ ^[0-9a-f]{40}$ ]] || return 0
  stamp="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  if test -n "$detail"; then
    printf '%s\t%s\t%s\t%s\n' "$stamp" "$code" "$sha" "$detail" >> logs/operations.tsv || log 'Could not write operation detail'
    return 0
  fi
  printf '%s\t%s\t%s\n' "$stamp" "$code" "$sha" >> logs/operations.tsv ||
    log "Could not write operation record: $code"
}
log() { echo "$(date '+%F %T') [web-app] $*"; }
load_env() {
  test -f .env || { log 'Missing private .env'; exit 1; }
  set -a; source .env; set +a
  # Only the foundation host opts into the shared privileged credential.
  # Downstream application web services retain their repository-scoped tokens.
  if test "${APP_BASE_PREVIEW:-}" = 1 && test -e "$HOME/.local/share/macmini-github"; then
    local shared_dir="$HOME/.local/share/macmini-github"
    local shared="$shared_dir/github-token"
    if test -e "$shared" || test -L "$shared"; then
      test ! -L "$shared_dir" && test -d "$shared_dir" &&
        test "$(stat -f '%u:%Lp' "$shared_dir")" = "$(id -u):700" &&
        test ! -L "$shared" && test -f "$shared" &&
        test "$(stat -f '%u:%Lp' "$shared")" = "$(id -u):600" ||
        { log 'Unsafe shared GitHub credential'; exit 1; }
      APP_GITHUB_TOKEN="$(cat "$shared")"
      [[ "$APP_GITHUB_TOKEN" =~ ^[A-Za-z0-9_-]{20,300}$ ]] ||
        { log 'Invalid shared GitHub credential'; exit 1; }
      export APP_GITHUB_TOKEN
    fi
  fi
  # Single-token mode is opt-in and overrides legacy names in the process
  # environment. No credentials are printed, copied into plists, or committed.
  if test -n "${APP_GITHUB_TOKEN:-}"; then
    export APP_GITHUB_PR_TOKEN="$APP_GITHUB_TOKEN"
    export APP_GITHUB_FEEDBACK_TOKEN="$APP_GITHUB_TOKEN"
  fi
  local proxy
  proxy="$(git config --local --get http.proxy || true)"
  if test -n "$proxy"; then
    export https_proxy="$proxy" HTTPS_PROXY="$proxy" http_proxy="$proxy" HTTP_PROXY="$proxy"
    export no_proxy="localhost,127.0.0.1,::1" NO_PROXY="localhost,127.0.0.1,::1"
  fi
  APP_ALLOW_BOOTSTRAP_LOCK="${APP_ALLOW_BOOTSTRAP_LOCK:-false}"
  APP_GITHUB_FEEDBACK_TOKEN="${APP_GITHUB_FEEDBACK_TOKEN:-}"
  test "$APP_PUBLIC_ORIGIN" = "$EXPECTED_ORIGIN" || { log 'Wrong origin'; exit 1; }
  test "$APP_COOKIE_SECURE" = "true" || { log 'Secure cookie required'; exit 1; }
  test "$APP_ALLOW_LOCAL_DEV" = "false" || { log 'Dev mode forbidden'; exit 1; }
  test -n "$APP_ADMIN_PASSWORD_HASH" || { log 'Password hash missing'; exit 1; }
  case "$DATABASE_URL" in postgresql+psycopg://*) ;; *) log 'PostgreSQL required'; exit 1;; esac
}
assert_main() {
  test "$(git branch --show-current)" = "${TRACK_BRANCH:-main}" || { log "Required branch: ${TRACK_BRANCH:-main}"; exit 1; }
  test -z "$(git status --porcelain --untracked-files=no)" || { log 'Tracked file modifications'; exit 1; }
}
dependencies() {
  local candidate=".venv.next.$$" previous=".venv.previous.$$" had_previous=0
  local python
  if test -x .venv/bin/python && .venv/bin/python -c 'import PIL, fastapi, sqlalchemy, uvicorn; from app import main' >/dev/null 2>&1; then
    return 0
  fi
  rm -rf "$candidate" "$previous" || return 1
  python3 -m venv "$candidate" || { rm -rf "$candidate"; return 1; }
  python="$candidate/bin/python"
  if ! "$python" -m pip install --disable-pip-version-check -r requirements.txt ||
     ! "$python" -c 'import PIL, fastapi, sqlalchemy, uvicorn; from app import main' ||
     ! "$python" -m scripts.apply_migrations --help >/dev/null 2>&1; then
    rm -rf "$candidate"
    return 1
  fi
  if test -e .venv; then
    mv .venv "$previous" || { rm -rf "$candidate"; return 1; }
    had_previous=1
  fi
  if ! mv "$candidate" .venv; then
    if test "$had_previous" = 1; then
      mv "$previous" .venv || return 1
    fi
    rm -rf "$candidate"
    return 1
  fi
  rm -rf "$previous"
}
check_schema() { .venv/bin/python -m scripts.apply_migrations --check; }
test_app() {
  # Never inherit application secrets or production database settings into tests.
  local sha output rc=0
  sha="$(git rev-parse HEAD)" || return 1
  output="$(mktemp "$ROOT/logs/.test-output.XXXXXX")" || return 1
  chmod 600 "$output" || { rm -f "$output"; return 1; }
  env -i PATH="$PATH" HOME="$HOME" LANG=C.UTF-8 DATABASE_URL=sqlite:///:memory: PYTHONDONTWRITEBYTECODE=1 \
    .venv/bin/python -m pytest -q --tb=no >"$output" 2>&1 || rc=$?
  # Export only fixed-schema test identities, not traceback, assertion values or raw output.
  .venv/bin/python -I -c '
import re,sys
from pathlib import Path
raw=Path(sys.argv[1]).read_text(errors="replace")
for line in raw.splitlines():
    m=re.fullmatch(r"FAILED (tests/[A-Za-z0-9_/]{1,120}\.py)::([A-Za-z_][A-Za-z0-9_]{0,79})(?:::([A-Za-z_][A-Za-z0-9_]{0,79}))?(?:\[[^\r\n]*\])?(?: - .*)?",line)
    if m:
        print(m[1]+"::"+m[2]+("::"+m[3] if m[3] else ""))
' "$output" > "$output.safe" || { rm -f "$output" "$output.safe"; return 1; }
  if test "$rc" != 0; then
    local identity
    while IFS= read -r identity; do
      record_operation host_test_case_failed "$sha" "$identity"
    done < "$output.safe"
    log "Host pytest failed (exit $rc); see structured test identities"
  fi
  rm -f "$output" "$output.safe"
  test "$rc" = 0 || return "$rc"
  env -i PATH="$PATH" HOME="$HOME" LANG=C.UTF-8 .venv/bin/python -m compileall -q app
}
build() {
  command -v npm >/dev/null || return 1
  node -e 'let [a,b]=process.versions.node.split(".").map(Number);process.exit(((a===20&&b>=19)||(a===22&&b>=12)||a>=24)?0:1)' || return 1
  if test -s frontend/package-lock.json; then
    npm --prefix frontend ci --no-audit --no-fund || return 1
  elif test "$APP_ALLOW_BOOTSTRAP_LOCK" = "true"; then
    npm --prefix frontend install --no-audit --no-fund || return 1
  else
    log 'Reviewed frontend/package-lock.json required'; return 1
  fi
  rm -rf frontend/dist.next || return 1
  (cd frontend && VITE_BUILD_SHA="$(git rev-parse HEAD)" npm run build -- --outDir dist.next) || return 1
  printf "%s\\n" "$(git rev-parse HEAD)" > frontend/dist.next/build-sha.txt || return 1
  test -s frontend/dist.next/index.html
}
health_once() {
  local expected="$1"
  curl --noproxy '*' -fsS --max-time 3 "http://127.0.0.1:$PORT/api/version" |
  .venv/bin/python -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get("running_sha")==sys.argv[1] else 1)' "$expected" 2>/dev/null &&
  curl --noproxy '*' -fsS --max-time 3 "http://127.0.0.1:$PORT/api/health" |
  .venv/bin/python -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get("status")=="ok" else 1)' 2>/dev/null &&
  curl --noproxy '*' -fsS --max-time 3 "http://127.0.0.1:$PORT/" | grep -q '/assets/' &&
  { if git show "$expected:deploy/macmini/manage.sh" | grep -q VITE_BUILD_SHA; then
      test "$(curl --noproxy '*' -fsS --max-time 3 "http://127.0.0.1:$PORT/build-sha.txt")" = "$expected"
    else
      test ! -f frontend/dist/build-sha.txt || test "$(curl --noproxy '*' -fsS --max-time 3 "http://127.0.0.1:$PORT/build-sha.txt")" = "$expected"
    fi; } &&
  test "$(curl --noproxy '*' -s -o /dev/null -w '%{http_code}' --max-time 3 "http://127.0.0.1:$PORT/api/v1/feedback")" = "401" &&
  test "$(curl --noproxy '*' -s -o /dev/null -w '%{http_code}' --max-time 3 "http://127.0.0.1:$PORT/api/session")" = "401"
}
health() {
  local expected="$1" i
  for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    if health_once "$expected"; then return 0; fi
    sleep 2
  done
  log "Health check failed for $expected on port $PORT"
  return 1
}
serve() {
  load_env
  export APP_RUNNING_SHA="$(git rev-parse HEAD)"
  if test "${APP_INIT_SYSTEM_SERVICE:-}" = "1"; then
    export APP_INIT_UPDATE_SIGNAL=1
  else
    export APP_UPDATER_TARGET="gui/$(id -u)/$UPDATER"
  fi
  check_schema || { local rc=$?; record_operation schema_check_failed "$APP_RUNNING_SHA"; return "$rc"; }
  if ! test -s frontend/dist/index.html; then
    record_operation frontend_missing "$APP_RUNNING_SHA"
    log 'Missing built frontend'
    return 1
  fi
  if test "${TRACK_BRANCH:-main}" = preview; then
    if ! .venv/bin/python -c 'import socket,sys; s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); s.bind(("127.0.0.1",int(sys.argv[1]))); s.close()' "$PORT"; then
      record_operation port_conflict "$APP_RUNNING_SHA"
      log "Port $PORT unavailable; refusing preview startup"
      return 1
    fi
  fi
  record_operation startup_ok "$APP_RUNNING_SHA"
  log "Startup checks passed for $APP_RUNNING_SHA"
  if test "${APP_INIT_SYSTEM_SERVICE:-}" = "1"; then
    printf '%s\n' "$$" > logs/web.pid
  fi
  exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "$PORT" --workers 1 --timeout-graceful-shutdown 5
}
agents() {
  export APP_AGENT_ROOT="$ROOT" APP_AGENT_DIR="$AGENT_DIR" APP_AGENT_PREFIX="$LABEL_PREFIX"
  .venv/bin/python - <<'PY'
import os, plistlib
from pathlib import Path
root=Path(os.environ["APP_AGENT_ROOT"])
dest=Path(os.environ["APP_AGENT_DIR"])
prefix=os.environ["APP_AGENT_PREFIX"]
acceptance=os.environ.get("APP_ACCEPTANCE_ID")
dest.mkdir(parents=True,exist_ok=True)
for label,cmd,extra in (
    (prefix+".web","serve",{"KeepAlive":{"SuccessfulExit":False}}),
    (prefix+".updater","update",{"StartInterval":300}),
    (prefix+".feedback","feedback",{"StartInterval":60})):
    p=dest/(label+".plist")
    spec={"Label":label,"RunAtLoad":True,"WorkingDirectory":str(root),
          "ProgramArguments":["/bin/bash",str(root/"deploy/macmini/manage.sh"),cmd],
          "StandardOutPath":str(root/"logs"/(cmd+".out.log")),
          "StandardErrorPath":str(root/"logs"/(cmd+".err.log")),**extra}
    if acceptance:
        spec["EnvironmentVariables"] = {
            "APP_ACCEPTANCE_ID": acceptance,
            "APP_ACCEPTANCE_PORT": os.environ["APP_ACCEPTANCE_PORT"],
        }
        if cmd != "serve":
            spec.pop("StartInterval")
            spec["RunAtLoad"] = False
    with p.open("wb") as f: plistlib.dump(spec,f)
    p.chmod(0o600)
PY
  local label
  for label in "$WEB" "$UPDATER" "$FEEDBACK"; do
    launchctl bootout "gui/$(id -u)/$label" >/dev/null 2>&1 || true
    launchctl bootstrap "gui/$(id -u)" "$AGENT_DIR/$label.plist" || return 1
  done
}
install() {
  assert_main; load_env
  dependencies
  check_schema
  test_app
  build
  # Reinstall must not nest dist.next inside an existing dist directory.
  # Preserve previous assets until the replacement has passed health checks.
  if test -d frontend/dist.previous; then
    log 'Previous asset backup exists; inspect before reinstall'; exit 1
  fi
  if test -d frontend/dist; then mv frontend/dist frontend/dist.previous; fi
  mv frontend/dist.next frontend/dist
  if ! agents || ! health "$(git rev-parse HEAD)"; then
    record_operation install_failed "$(git rev-parse HEAD)"
    log 'Install health failed; restoring previous frontend assets'
    rm -rf frontend/dist
    if test -d frontend/dist.previous; then mv frontend/dist.previous frontend/dist; fi
    launchctl kickstart -k "gui/$(id -u)/$WEB" || true
    exit 1
  fi
  rm -rf frontend/dist.previous
  record_operation install_ok "$(git rev-parse HEAD)"
  log "Installed $(git rev-parse HEAD)"
}
feedback() {
  load_env
  local result rc=0 tasks_rc=0
  if test -n "$APP_GITHUB_FEEDBACK_TOKEN"; then
    .venv/bin/python -m scripts.sync_development_tasks --active || log "Active PR sync failed"
  fi
  # Task refresh must run even if feedback submission fails. Both use the
  # server-only GitHub token; without it, retain the previous task snapshot.
  if test -n "$APP_GITHUB_FEEDBACK_TOKEN"; then
    if test "${TRACK_BRANCH:-main}" = preview; then
      export APP_PREVIEW_FEEDBACK=1
    fi
    result="$(.venv/bin/python -m scripts.sync_feedback)" || rc=$?
    if test -n "$result"; then printf '%s\n' "$result"; fi
    if test "$rc" -ne 0; then
      record_operation feedback_failed "$(git rev-parse HEAD)"
    elif [[ "$result" == *"Synced feedback "* ]]; then
      record_operation feedback_ok "$(git rev-parse HEAD)"
    fi
  fi
  if test -n "$APP_GITHUB_FEEDBACK_TOKEN"; then
    .venv/bin/python -m scripts.sync_development_tasks || tasks_rc=$?
  else
    log 'GitHub token absent; task snapshot remains stale'
  fi
  if test "$tasks_rc" -ne 0; then
    log 'Development task sync did not complete'
  fi
  if test "$rc" -ne 0; then return "$rc"; fi
  return "$tasks_rc"
}
restart_web() {
  if test "${APP_INIT_SYSTEM_SERVICE:-}" != "1"; then
    launchctl kickstart -k "gui/$(id -u)/$WEB"
    return
  fi
  # Use the exact launchd job, never the stale web.pid file. Capture the
  # original process identity so escalation cannot target a replacement.
  local job pid process cwd started owner attempt observed
  job="$(launchctl print "system/$WEB" 2>/dev/null)" || {
    log "Web job is not accessible: system/$WEB"; return 1;
  }
  pid="$(printf '%s\n' "$job" | awk '$1 == "pid" && $2 == "=" {print $3; exit}')"
  if test -z "$pid"; then
    log "Web job has no PID; waiting for launchd KeepAlive"
    return 0
  fi
  [[ "$pid" =~ ^[1-9][0-9]*$ ]] || return 1
  process="$(ps -p "$pid" -o command=)" || {
    log 'Web process exited during restart; waiting for launchd'; return 0;
  }
  [[ "$process" == *"uvicorn app.main:app --host 127.0.0.1 --port $PORT --workers 1"* ]] || {
    log 'Web job is starting or process identity differs; no signal sent'; return 1;
  }
  cwd="$(lsof -nP -a -p "$pid" -d cwd -Fn | sed -n 's/^n//p')" || return 1
  test "$cwd" = "$ROOT" || {
    log 'Web PID working directory mismatch; refusing signal'; return 1;
  }
  started="$(ps -p "$pid" -o lstart=)" || return 1
  owner="$(ps -p "$pid" -o uid= | tr -d '[:space:]')" || return 1
  test -n "$started" && test "$owner" = "$(id -u)" || return 1
  kill -TERM "$pid" || { log 'Web signal failed; checking health'; return 1; }
  # Existing versions may lack Uvicorn's graceful shutdown deadline. Give
  # them a bounded grace period, then revalidate every identity attribute.
  for attempt in 1 2 3 4 5 6 7 8 9 10; do
    sleep 1
    job="$(launchctl print "system/$WEB" 2>/dev/null)" || return 1
    observed="$(printf '%s\n' "$job" | awk '$1 == "pid" && $2 == "=" {print $3; exit}')"
    test "$observed" = "$pid" || return 0
    kill -0 "$pid" 2>/dev/null || return 0
  done
  test "$(ps -p "$pid" -o lstart=)" = "$started" || return 0
  test "$(ps -p "$pid" -o uid= | tr -d '[:space:]')" = "$owner" || return 1
  test "$(ps -p "$pid" -o command=)" = "$process" || return 1
  test "$(lsof -nP -a -p "$pid" -d cwd -Fn | sed -n 's/^n//p')" = "$ROOT" || return 1
  job="$(launchctl print "system/$WEB" 2>/dev/null)" || return 1
  observed="$(printf '%s\n' "$job" | awk '$1 == "pid" && $2 == "=" {print $3; exit}')"
  test "$observed" = "$pid" || return 0
  log "Graceful shutdown exceeded 10 seconds for verified $WEB; escalating to SIGKILL"
  kill -KILL "$pid" || { log 'Escalation failed; checking health'; return 1; }
}
rollback() {
  local old="$1" bad="$2" restored=1
  if test "${3:-hold}" = "hold"; then
    printf '%s\n' "$bad" > .webapp-failed-sha
  fi
  if test -d frontend/dist.previous; then
    rm -rf frontend/dist || restored=0
    mv frontend/dist.previous frontend/dist || restored=0
  fi
  rm -rf frontend/dist.next || restored=0
  git reset --hard "$old" || restored=0
  .venv/bin/python -m pip install -q -r requirements.txt || restored=0
  if test "${3:-hold}" = "hold"; then record_operation rollback "$bad"; fi
  # A failure before restart may leave the previous service healthy.
  # Do not stop that process unnecessarily. Otherwise wait for launchd.
  if test "$restored" = 1 && { health_once "$old" || { restart_web || log 'Rollback restart request failed'; health "$old"; }; }; then
    record_operation rollback_ok "$old"
    log "ROLLBACK_OK $old; held candidate $bad"
    return 0
  fi
  printf '%s\n' "$bad" > .webapp-failed-sha
  record_operation rollback_failed "$bad"
  log "ROLLBACK_FAILED $old; inspect system/$WEB and health; manual recovery required"
  return 1
}
# Classify the captured error without ever writing raw Git stderr to the UI.
fetch_reason() {
  local message
  message="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  case "$message" in
    *"proxy"*|*"connect tunnel"*) echo proxy;;
    *"permission denied"*|*"authentication failed"*|*"could not read username"*|*"host key verification failed"*) echo authentication;;
    *"repository not found"*|*"access denied"*|*"403"*|*"401"*) echo repository_access;;
    *"could not resolve"*|*"name or service not known"*) echo dns;;
    *"timed out"*|*"timeout"*) echo timeout;;
    *"connection reset"*|*"connection refused"*|*"failed to connect"*|*"network is unreachable"*|*"remote end hung up"*|*"early eof"*|*"502"*|*"503"*|*"504"*) echo network;;
    *) echo unknown;;
  esac
}
fetch_with_retry() {
  local attempt=1 err reason rc=1
  while test "$attempt" -le 3; do
    if err="$(GIT_TERMINAL_PROMPT=0 GIT_SSH_COMMAND='ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=10 -o ServerAliveCountMax=2' git -c http.lowSpeedLimit=1 -c http.lowSpeedTime=20 fetch --quiet origin "${TRACK_BRANCH:-main}" 2>&1)"; then
      return 0
    else
      rc=$?
    fi
    reason="$(fetch_reason "$err")"
    UPDATE_DETAIL="$reason"
    log "Git fetch attempt $attempt/3 failed: $reason"
    case "$reason" in authentication|repository_access) return "$rc";; esac
    if test "$attempt" -lt 3; then
      record_operation update_fetch_retry "$UPDATE_SHA" "$reason"
      sleep "$((attempt * 2))"
    fi
    attempt=$((attempt + 1))
  done
  return "$rc"
}
finish_update() {
  local rc="$1"
  if test "$rc" != 0; then
    record_operation "$UPDATE_FAILURE" "$UPDATE_SHA" "${UPDATE_DETAIL:-}"
  fi
  rmdir "$LOCK" 2>/dev/null || true
}
update() {
  local old new
  old="$(git rev-parse HEAD)"
  if ! mkdir "$LOCK" 2>/dev/null; then
    record_operation update_locked "$old"
    log 'Update locked'; exit 0
  fi
  UPDATE_SHA="$old"
  UPDATE_FAILURE=update_preflight_failed
  UPDATE_DETAIL=""
  trap 'finish_update "$?"' EXIT
  record_operation update_started "$old"
  assert_main; load_env
  UPDATE_FAILURE=update_fetch_failed
  fetch_with_retry || exit $?
  UPDATE_DETAIL=""
  new="$(git rev-parse "origin/${TRACK_BRANCH:-main}")"
  UPDATE_SHA="$new"
  if test "$old" = "$new"; then
    record_operation update_no_change "$new"
    log "Already at tracked branch SHA $new"; exit 0
  fi
  if test -f .webapp-failed-sha && test "$(cat .webapp-failed-sha)" = "$new"; then
    record_operation update_held "$new"
    log "Failed SHA held: $new"; exit 0
  fi
  UPDATE_FAILURE=update_preflight_failed
  git merge-base --is-ancestor "$old" "$new" || { log 'Non-fast-forward blocked'; exit 1; }
  # Leave recoverable assets intact before any step can enter rollback.
  if test -e frontend/dist.previous; then
    log "Previous frontend backup exists; manual recovery required"
    exit 1
  fi
  record_operation update_candidate "$new"
  git merge --ff-only "$new"
  # Never auto-migrate or pretend DB rollback is transactional.
  # Dependencies must be installed before importing the new migration checker.
  UPDATE_FAILURE=update_dependencies_failed
  record_operation update_dependencies "$new"
  if ! dependencies; then
    rollback "$old" "$new"; exit 1
  fi
  # Pending migration is an operational prerequisite, not a defective release.
  # Do not blacklist the SHA: after a reviewed manual migration the same SHA
  # must be eligible on the next update tick.
  UPDATE_FAILURE=update_schema_failed
  if ! check_schema; then
    record_operation update_blocked "$new"
    log "Schema gate blocked $new; back up DB and apply reviewed migration manually"
    rollback "$old" "$new" retryable; exit 1
  fi
  UPDATE_FAILURE=update_test_build_failed
  record_operation update_test_build "$new"
  if ! test_app; then
    UPDATE_DETAIL=host_test_failed
    rollback "$old" "$new"; exit 1
  fi
  if ! build; then
    UPDATE_DETAIL=frontend_build_failed
    rollback "$old" "$new"; exit 1
  fi
  UPDATE_FAILURE=update_restart_failed
  if test -d frontend/dist; then
    if ! mv frontend/dist frontend/dist.previous; then rollback "$old" "$new"; exit 1; fi
  fi
  if ! mv frontend/dist.next frontend/dist; then rollback "$old" "$new"; exit 1; fi
  record_operation update_restarting "$new"
  restart_web || log "Restart request failed; checking candidate service"
  if ! health "$new"; then
    rollback "$old" "$new"; exit 1
  fi
  rm -rf frontend/dist.previous .webapp-failed-sha
  record_operation update_ok "$new"
  log "DEPLOY_OK $new (verify public HTTPS separately)"
}
prepare() {
  assert_main; load_env
  dependencies
  .venv/bin/python -m scripts.apply_migrations
  check_schema
  test_app
  build
  mv frontend/dist.next frontend/dist
}
case "$1" in
  prepare) prepare;;
  verify)
    health "$(git rev-parse HEAD)"
    record_operation install_ok "$(git rev-parse HEAD)"
    ;;
  serve) serve;;
  install) install;;
  update) update;;
  feedback) feedback;;
  status) launchctl print "gui/$(id -u)/$WEB" | head -n 30;;
  *) echo 'Usage: manage.sh {install|serve|update|feedback|status}' >&2; exit 2;;
esac
