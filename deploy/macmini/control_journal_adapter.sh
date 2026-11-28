#!/bin/sh
# Journal adapter called by manage.sh.
# Isolation is the Python file-descriptor root, not a /tmp string prefix.
# This script does not call launchctl, git, or a service switch.
set -eu
here=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
repo=$(CDPATH= cd -- "$here/../.." && pwd)
cd "$repo"
export PYTHONPATH="$repo${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m app.control_joint.w07_adapter "$@"
