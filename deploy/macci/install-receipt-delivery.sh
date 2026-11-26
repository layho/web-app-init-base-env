#!/bin/bash
set -euo pipefail
umask 077
[[ $# -eq 1 && "$1" =~ ^[0-9a-f]{64}$ ]] || { echo "Usage: $0 <independently verified runner SHA-256>"; exit 2; }
expected_hash="$1"
cd "$(dirname "$0")"
[[ "$(uname -s)" == Darwin ]] || { echo 'macOS required'; exit 1; }
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
root="$HOME/.local/share/macmini-pr-tests"
python="$(command -v python3)"
"$python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else "Python 3.10+ required")'
label="com.layho.macci-receipt-delivery"
domain="gui/$(id -u)"
plist="$HOME/Library/LaunchAgents/$label.plist"
runner="$root/runner.py"
[[ -f "$runner" ]] || { echo 'Install the reviewed MacCI runtime first'; exit 1; }
"$python" - "$root" "$expected_hash" <<'PY'
import hashlib, os, stat, sys
from pathlib import Path
root=Path(sys.argv[1])
shared=Path.home()/".local/share/macmini-github"
try:
    shared_info=shared.lstat()
except FileNotFoundError:
    token=root/"github-token"
else:
    if not stat.S_ISDIR(shared_info.st_mode) or shared_info.st_uid != os.getuid() or shared_info.st_mode & 0o077:
        raise SystemExit("Unsafe shared credential directory")
    token=shared/"github-token"
for path in (root, token, root/"runner.py"):
    info=path.lstat()
    if path == root:
        valid=stat.S_ISDIR(info.st_mode)
    else:
        valid=stat.S_ISREG(info.st_mode)
    if not valid or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise SystemExit(f"Unsafe MacCI runtime path: {path.name}")
fd=os.open(root/"runner.py", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
with os.fdopen(fd,"rb") as stream:
    info=os.fstat(stream.fileno())
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise SystemExit("Unsafe opened runner")
    digest=hashlib.sha256()
    for block in iter(lambda: stream.read(65536), b""):
        digest.update(block)
    if digest.hexdigest() != sys.argv[2]:
        raise SystemExit("Installed runner SHA-256 mismatch; refusing to load")
PY
launchctl print "$domain" >/dev/null || { echo 'No active Mac GUI login session'; exit 1; }
if launchctl print "$domain/$label" >/dev/null 2>&1; then
    echo 'Receipt sender already loaded; refusing to overwrite a running service.'
    exit 1
fi
mkdir -p "$HOME/Library/LaunchAgents"
"$python" - "$root" "$python" "$plist" <<'PY'
import plistlib, sys
from pathlib import Path
root, python, name = sys.argv[1:]
job = {"Label": "com.layho.macci-receipt-delivery",
       "ProgramArguments": [python, root+"/runner.py", "--receipts-only"],
       "WorkingDirectory": root, "StartInterval": 60, "RunAtLoad": True,
       "EnvironmentVariables": {"HOME": str(Path.home()), "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
       "StandardOutPath": root+"/receipt-delivery.log", "StandardErrorPath": root+"/receipt-delivery.stderr.log"}
path=Path(name)
with path.open("xb") as f:
    plistlib.dump(job,f)
path.chmod(0o600)
PY
if ! plutil -lint "$plist" || ! launchctl bootstrap "$domain" "$plist"; then
    if ! launchctl print "$domain/$label" >/dev/null 2>&1; then
        rm -f -- "$plist"
    fi
    echo "Receipt service installation failed; inspect launchctl before retrying." >&2
    exit 1
fi
echo 'Independent receipt sender loaded: every 60 seconds; no Docker or test worker lock.'
echo "Log: $root/receipt-delivery.log"
