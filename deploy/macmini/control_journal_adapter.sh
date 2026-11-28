#!/bin/sh
# Isolated adapter entry. manage.sh does not source this file.
# It refuses every invocation that is not an explicit temporary-directory probe.
set -eu
if [ "${V060_JOINT_EXECUTE:-}" != "isolated" ]; then
  echo "BLOCKED: joint deploy adapter is not authorized" >&2
  exit 2
fi
case "${V060_CONTROL_ROOT:-}" in
  /tmp/*) ;;
  *)
    echo "BLOCKED: control root must be a temporary directory" >&2
    exit 2
    ;;
esac
echo "isolated probe only; no service switch"
