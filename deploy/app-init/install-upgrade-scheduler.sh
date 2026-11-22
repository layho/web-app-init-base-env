#!/bin/bash
# Select an existing compatible interpreter before importing scheduler modules.
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
HERE="$(cd "$(dirname "$0")" && pwd -P)"
ROOT="$(cd "$HERE/../.." && pwd -P)"
for candidate in "$(command -v python3 || true)" /opt/homebrew/bin/python3 /usr/local/bin/python3 "$ROOT/.venv/bin/python"; do
    test -n "$candidate" && test -x "$candidate" || continue
    if "$candidate" -B -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' >/dev/null 2>&1; then
        printf 'Using Python: %s\n' "$candidate"
        exec "$candidate" -B "$HERE/install-upgrade-scheduler.py" "$@"
    fi
done
echo 'No existing Python 3.10+ found; no scheduler files changed.' >&2
exit 1
