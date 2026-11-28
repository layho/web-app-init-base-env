#!/bin/sh
# Journal adapter called by manage.sh.
# Default deny is a separate exit from the isolated record/plan/status path.
# This script does not call launchctl, git, or a service switch.
set -eu
command="${1:-status}"
if [ "${V060_JOINT_EXECUTE:-}" != "isolated" ]; then
  echo "BLOCKED: joint deploy adapter is not authorized" >&2
  exit 2
fi
root="${V060_CONTROL_ROOT:-}"
case "$root" in
  /tmp/*|/private/tmp/*|/var/folders/*)
    ;;
  *)
    echo "BLOCKED: control root must be a temporary directory" >&2
    exit 2
    ;;
esac
case "$root" in
  *..*)
    echo "BLOCKED: control root must be a temporary directory" >&2
    exit 2
    ;;
esac
if [ -L "$root" ] || [ ! -d "$root" ]; then
  echo "BLOCKED: control root is not a real temporary directory" >&2
  exit 2
fi
journal="$root/control-journal.tsv"
case "$command" in
  status)
    if [ -f "$journal" ]; then
      cat "$journal"
    else
      echo "journal-empty"
    fi
    ;;
  plan|record)
    printf '%s\t%s\t%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$command" "${V060_JOINT_CALLER:-adapter}" >> "$journal"
    echo "$command"
    ;;
  *)
    echo "BLOCKED: command is not in the isolated allowlist" >&2
    exit 2
    ;;
esac
