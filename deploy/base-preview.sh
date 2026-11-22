#!/bin/bash
# Deploy the foundation's own isolated preview, without initializing or pushing a new repository.
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
here="$(cd "$(dirname "$0")" && pwd -P)"
for python in python3.12 python3.13 python3.14 python3.11 python3.10 python3; do
  if "$python" -c 'import sys; sys.exit(sys.version_info < (3, 10))' >/dev/null 2>&1; then
    exec "$python" "$here/base_preview.py" "$@"
  fi
done
echo '需要 Python 3.10+，请安装 Homebrew python@3.12。' >&2
exit 1
