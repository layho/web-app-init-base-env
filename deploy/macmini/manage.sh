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
  local code="$1" sha="$2" stamp
  [[ "$sha" =~ ^[0-9a-f]{40}$ ]] || return 0
  stamp="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  printf '%s\t%s\t%s\n' "$stamp" "$code" "$sha" >> logs/operations.tsv ||
    log "Could not write operation record: $code"
}
log() { echo "$(date '+%F %T') [web-app] $*"; }
load_env() {
  test -f .env || { log 'Missing private .env'; exit 1; }
  set -a; source .env; set +a
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
  test -x .venv/bin/python || python3 -m venv .venv
  .venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt
}
check_schema() { .venv/bin/python -m scripts.apply_migrations --check; }
test_app() { .venv/bin/python -m pytest -q && .venv/bin/python -m compileall -q app; }
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
  (cd frontend && npm run build -- --outDir dist.next) || return 1
  test -s frontend/dist.next/index.html
}
health() {
  local expected="$1" i
  for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    # Validate exact JSON values rather than matching a SHA substring.
    if curl -fsS --max-time 3 "http://127.0.0.1:$PORT/api/version" |
       .venv/bin/python -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get("running_sha")==sys.argv[1] else 1)' "$expected" 2>/dev/null &&
       curl -fsS --max-time 3 "http://127.0.0.1:$PORT/api/health" |
       .venv/bin/python -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get("status")=="ok" else 1)' 2>/dev/null &&
       curl -fsS --max-time 3 "http://127.0.0.1:$PORT/" | grep -q '/assets/' &&
       test "$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://127.0.0.1:$PORT/api/v1/feedback")" = "401" &&
       test "$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://127.0.0.1:$PORT/api/session")" = "401"; then
      return 0
    fi
    sleep 2
  done
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
    if ! .venv/bin/python -c 'import socket,sys; s=socket.socket(); s.bind(("127.0.0.1",int(sys.argv[1]))); s.close()' "$PORT"; then
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
  exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "$PORT" --workers 1
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
  local pid process
  read -r pid < logs/web.pid || return 1
  [[ "$pid" =~ ^[1-9][0-9]*$ ]] || return 1
  process="$(ps -p "$pid" -o command=)" || return 1
  [[ "$process" == *"uvicorn app.main:app --host 127.0.0.1 --port $PORT --workers 1"* ]] || return 1
  kill -TERM "$pid"
}
rollback() {
  local old="$1" bad="$2"
  if test "${3:-hold}" = "hold"; then
    printf '%s\n' "$bad" > .webapp-failed-sha
  fi
  if test -d frontend/dist.previous; then
    rm -rf frontend/dist
    mv frontend/dist.previous frontend/dist
  fi
  rm -rf frontend/dist.next
  git reset --hard "$old" || true
  .venv/bin/python -m pip install -q -r requirements.txt || true
  restart_web || true
  if test "${3:-hold}" = "hold"; then record_operation rollback "$bad"; fi
  log "Rollback to $old; failed $bad"
}
update() {
  assert_main; load_env
  mkdir "$LOCK" 2>/dev/null || { log 'Update locked'; exit 0; }
  trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT
  local old new
  old="$(git rev-parse HEAD)"
  GIT_TERMINAL_PROMPT=0 git fetch --quiet origin "${TRACK_BRANCH:-main}"
  new="$(git rev-parse "origin/${TRACK_BRANCH:-main}")"
  test "$old" != "$new" || exit 0
  if test -f .webapp-failed-sha && test "$(cat .webapp-failed-sha)" = "$new"; then
    log "Failed SHA held: $new"; exit 0
  fi
  git merge-base --is-ancestor "$old" "$new" || { log 'Non-fast-forward blocked'; exit 1; }
  # Leave recoverable assets intact before any step can enter rollback.
  if test -e frontend/dist.previous; then
    log "Previous frontend backup exists; manual recovery required"
    exit 1
  fi
  git merge --ff-only "$new"
  # Never auto-migrate or pretend DB rollback is transactional.
  # Dependencies must be installed before importing the new migration checker.
  if ! dependencies; then
    rollback "$old" "$new"; exit 1
  fi
  # Pending migration is an operational prerequisite, not a defective release.
  # Do not blacklist the SHA: after a reviewed manual migration the same SHA
  # must be eligible on the next update tick.
  if ! check_schema; then
    record_operation update_blocked "$new"
    log "Schema gate blocked $new; back up DB and apply reviewed migration manually"
    rollback "$old" "$new" retryable; exit 1
  fi
  if ! test_app || ! build; then
    rollback "$old" "$new"; exit 1
  fi
  if test -d frontend/dist; then mv frontend/dist frontend/dist.previous; fi
  mv frontend/dist.next frontend/dist
  if ! restart_web || ! health "$new"; then
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
