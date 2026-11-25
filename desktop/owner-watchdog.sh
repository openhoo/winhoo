#!/usr/bin/env bash
set -eu

heartbeat=$1
generation=$2
timeout=${3:-90}
interval=${4:-5}
target=${5:-1}
if [[ "$target" == 1 ]]; then
  exec 2>/proc/1/fd/2
fi
now_seconds() {
  if [[ -r /proc/uptime ]]; then
    read -r uptime _ < /proc/uptime
    printf '%s\n' "${uptime%%.*}"
  else
    date +%s
  fi
}
last_change=$(now_seconds)
previous=''

while sleep "$interval"; do
  actual=''
  if [[ -r "$heartbeat" ]]; then
    tick=''
    read -r actual tick < "$heartbeat" || true
    # A new owner supersedes this watcher. It must not stop the new session.
    [[ "$actual" == "$generation" ]] || exit 0
    if [[ "$tick" != "$previous" ]]; then
      previous=$tick
      last_change=$(now_seconds)
    fi
  fi
  now=$(now_seconds)
  if (( now - last_change >= timeout )); then
    echo 'WinHoo desktop owner heartbeat expired; requesting ACPI shutdown.' >&2
    kill -TERM "$target"
    exit 0
  fi
done
