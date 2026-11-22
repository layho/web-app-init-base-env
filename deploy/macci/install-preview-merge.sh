#!/bin/bash
set -euo pipefail
umask 077
cd "$(dirname "$0")"
[[ "$(uname -s)" == Darwin ]] || { echo 'macOS required'; exit 1; }
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
root="$HOME/.local/share/macmini-pr-tests"
python="$(command -v python3)"
label="com.layho.macci-preview-merge"
domain="gui/$(id -u)"
plist="$HOME/Library/LaunchAgents/$label.plist"
[[ -f "$root/github-token" ]] || { echo 'Existing MacCI credential required'; exit 1; }
launchctl print "$domain" >/dev/null || { echo 'No active Mac GUI login session for LaunchAgent'; exit 1; }
"$python" -m unittest -v test_merge_preview
"$python" merge_preview.py --dry-run
git ls-remote git@github.com:layho/web-app-init-base-env.git refs/heads/preview >/dev/null
if launchctl print "$domain/$label" >/dev/null 2>&1; then
    echo 'Coordinator already installed; do not replace a running process.'
    exit 1
fi
mkdir -p "$HOME/Library/LaunchAgents"
install -m 700 merge_preview.py "$root/merge_preview.py"
"$python" - "$root" "$python" "$plist" <<'PY'
import plistlib, sys
from pathlib import Path
root, python, name = sys.argv[1:]
job = {"Label": "com.layho.macci-preview-merge", "ProgramArguments": [python, root+"/merge_preview.py"],
       "WorkingDirectory": root, "StartInterval": 60, "RunAtLoad": True,
       "EnvironmentVariables": {"HOME": str(Path.home()), "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
       "StandardOutPath": root+"/preview-merge.log", "StandardErrorPath": root+"/preview-merge.stderr.log"}
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
echo 'Preview merge coordinator loaded: every 60 seconds; main untouched.'
echo "Log: $root/preview-merge.log"
