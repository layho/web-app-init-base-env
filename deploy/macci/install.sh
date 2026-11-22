#!/bin/bash
set -euo pipefail
umask 077
cd "$(dirname "$0")"
if [[ "$(uname -s)" != Darwin ]]; then echo "This installer is for macOS." >&2; exit 1; fi
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
python="$(command -v python3)"
docker info --format '{{.ServerVersion}}'
"$python" -m unittest -v test_runner
root="$HOME/.local/share/macmini-pr-tests"
label="com.layho.macci"
domain="gui/$(id -u)"
plist="$HOME/Library/LaunchAgents/$label.plist"
mkdir -p "$root" "$HOME/Library/LaunchAgents"
chmod 700 "$root"
# Do not replace a running service on an update.
if launchctl print "$domain/$label" >/dev/null 2>&1; then
    echo "Existing test service is loaded. Stop it when idle before reinstalling." >&2
    exit 1
fi
install -m 700 runner.py "$root/runner.py"
if [[ ! -f "$root/github-token" ]]; then
    "$python" "$root/runner.py" --configure
fi
"$python" - "$root" "$python" "$plist" <<'PY'
import os, plistlib, sys
from pathlib import Path
root, python, filename = sys.argv[1:]
job = {
    "Label": "com.layho.macci",
    "ProgramArguments": [python, root + "/runner.py"],
    "WorkingDirectory": root,
    "StartInterval": 60,
    "RunAtLoad": False,
    "EnvironmentVariables": {
        "HOME": str(Path.home()),
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
    },
    "StandardOutPath": root + "/worker.stdout.log",
    "StandardErrorPath": root + "/worker.stderr.log",
}
if os.environ.get("DOCKER_HOST"):
    job["EnvironmentVariables"]["DOCKER_HOST"] = os.environ["DOCKER_HOST"]
path = Path(filename)
if path.exists():
    with path.open("rb") as f:
        if plistlib.load(f).get("Label") != job["Label"]:
            raise SystemExit("Refusing to replace unrelated plist")
with path.open("wb") as out:
    plistlib.dump(job, out)
path.chmod(0o600)
PY
plutil -lint "$plist"
if launchctl print "$domain" >/dev/null 2>&1; then
    launchctl bootstrap "$domain" "$plist"
    echo "Test service loaded (every 60 seconds)."
else
    echo "No GUI login session: plist installed; service will load upon GUI login."
    echo "For now run the worker manually using the command below."
fi
printf 'Run PR #14 now: %q %q --repo layho/web-app-init-base-env --pr 14 --force\n' "$python" "$root/runner.py"
echo "Logs: $root"
