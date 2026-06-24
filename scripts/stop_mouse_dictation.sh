#!/usr/bin/env bash
set -euo pipefail

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
pid_file="$root/state/x11_mouse_dictation.pid"
unit="${PERSONAL_STT_MOUSE_UNIT:-personal-stt-mouse-dictation}"

systemctl --user stop "$unit.service" >/dev/null 2>&1 || true
systemctl --user reset-failed "$unit.service" >/dev/null 2>&1 || true

if [[ -f "$pid_file" ]]; then
  pid="$(cat "$pid_file")"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid"
    echo "stopped mouse dictation daemon pid=$pid"
  fi
  rm -f "$pid_file"
fi

pkill -f "$root/scripts/x11_mouse_dictation.py" || true
echo "stopped any matching mouse dictation daemon"
