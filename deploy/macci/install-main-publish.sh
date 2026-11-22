#!/bin/bash
set -euo pipefail
umask 077
cd "$(dirname "$0")"
[[ "$(uname -s)" == Darwin ]] || { echo 'macOS required'; exit 1; }
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
root="$HOME/.local/share/macmini-pr-tests"
python="$(command -v python3)"
label="com.layho.macci-main-publish"
domain="gui/$(id -u)"
plist="$HOME/Library/LaunchAgents/$label.plist"
[[ -f "$root/github-token" && -f "$root/merge_preview.py" ]] || { echo 'Install existing MacCI and preview coordinator first'; exit 1; }
cmp -s merge_preview.py "$root/merge_preview.py" || { echo 'Installed preview coordinator differs from reviewed version; stop and inspect'; exit 1; }
launchctl print "$domain" >/dev/null || { echo 'No active Mac GUI login session'; exit 1; }
if launchctl print "$domain/$label" >/dev/null 2>&1; then
    echo 'Main publisher already loaded; refusing to overwrite a running service.'
    exit 1
fi
"$python" -m unittest -v test_publish_main
"$python" publish_main.py --dry-run
git ls-remote git@github.com:layho/web-app-init-base-env.git refs/heads/main >/dev/null
mkdir -p "$HOME/Library/LaunchAgents"
install -m 700 publish_main.py "$root/publish_main.py"
"$python" - "$root" "$python" "$plist" <<'PY'
import plistlib, sys
from pathlib import Path
root, python, name = sys.argv[1:]
job = {"Label": "com.layho.macci-main-publish",
       "ProgramArguments": [python, root+"/publish_main.py"],
       "WorkingDirectory": root, "StartInterval": 60, "RunAtLoad": True,
       "EnvironmentVariables": {"HOME": str(Path.home()), "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
       "StandardOutPath": root+"/main-publish.log", "StandardErrorPath": root+"/main-publish.stderr.log"}
path=Path(name)
if path.exists():
    with path.open("rb") as f:
        if plistlib.load(f).get("Label") != job["Label"]:
            raise SystemExit("Refusing to replace unrelated LaunchAgent")
with path.open("wb") as f:
    plistlib.dump(job,f)
path.chmod(0o600)
PY
plutil -lint "$plist"
launchctl bootstrap "$domain" "$plist"
echo 'Main publisher loaded: every 60 seconds; only locally accepted exact candidates.'
echo "Log: $root/main-publish.log"
