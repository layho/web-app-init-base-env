#!/bin/bash
# Run the isolated acceptance with a compatible Python on macOS.
set -euo pipefail

install_python=false
feedback_only=false
entry=acceptance.py
if test "${1:-}" = "--release"; then
  entry=release.py
  shift
else
  for arg in "$@"; do
    case "$arg" in
      --install-python) install_python=true ;;
      --feedback-only) feedback_only=true ;;
      *) echo "用法：bash acceptance.sh [--install-python] [--feedback-only] 或 --release PR SHA" >&2; exit 2 ;;
    esac
  done
  set --
fi

export PATH="$PATH:/opt/homebrew/bin:/usr/local/bin"
script_dir="$(cd "$(dirname "$0")" && pwd -P)"

usable_python() {
  "$1" -c 'import ssl, sys, venv; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1
}

run() {
  local python="$1"
  shift
  if "$feedback_only"; then
    exec "$python" "$script_dir/$entry" --feedback-only
  fi
  exec "$python" "$script_dir/$entry" "$@"
}

for python in python3 python3.12 python3.13 python3.14 python3.11 python3.10 \
  /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  if usable_python "$python"; then
    run "$python" "$@"
  fi
done

if command -v brew >/dev/null; then
  prefix="$(brew --prefix python@3.12)"
  python="$prefix/bin/python3.12"
  if ! usable_python "$python" && "$install_python"; then
    echo "正在通过 Homebrew 安装 Python 3.12…"
    brew install python@3.12
  fi
  if usable_python "$python"; then
    run "$python" "$@"
  fi
  echo "找不到可用的 Python 3.10+。可加 --install-python 由 Homebrew 安装 Python 3.12。" >&2
else
  echo "找不到可用的 Python 3.10+ 或 Homebrew。请安装 Python 3.12 后重跑。" >&2
fi
exit 1
