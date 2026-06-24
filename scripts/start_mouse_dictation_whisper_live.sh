#!/usr/bin/env bash
set -euo pipefail

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
pid_file="$root/state/x11_mouse_dictation.pid"
log_file="$root/logs/x11_mouse_dictation.daemon.out"
unit="${PERSONAL_STT_MOUSE_UNIT:-personal-stt-mouse-dictation}"
python_bin="${PERSONAL_STT_PYTHON:-}"

mkdir -p "$root/state" "$root/logs"

if [[ -z "$python_bin" ]]; then
  if [[ -x "$root/.venv/bin/python" ]]; then
    python_bin="$root/.venv/bin/python"
  elif [[ -x "$root/.venv-stt/bin/python" ]]; then
    python_bin="$root/.venv-stt/bin/python"
  elif [[ -x "$HOME/.venv/bin/python" ]]; then
    python_bin="$HOME/.venv/bin/python"
  else
    python_bin="$(command -v python3 || command -v python)"
  fi
fi

if command -v systemctl >/dev/null && systemctl --user is-active --quiet "$unit.service"; then
  echo "mouse dictation daemon already running as $unit.service"
  exit 0
fi

if [[ -f "$pid_file" ]]; then
  old_pid="$(cat "$pid_file")"
  if [[ -n "$old_pid" ]] && kill -0 "$old_pid" 2>/dev/null; then
    echo "mouse dictation daemon already running pid=$old_pid"
    exit 0
  fi
  rm -f "$pid_file"
fi

cmd=(
  "$python_bin"
  "$root/scripts/x11_mouse_dictation.py"
  --start-button 9
  --commit-button 8
  --modifier shift
  --record-label whisper-live-record
  --commit-label whisper-live-commit
  --record-command "/usr/bin/fish $root/scripts/ptt_press_profile.fish current"
  --commit-command "/usr/bin/fish $root/scripts/ptt_release_commit.fish"
)

if command -v systemd-run >/dev/null; then
  env_args=(--setenv="DISPLAY=${DISPLAY:-:0}")
  if [[ -n "${XAUTHORITY:-}" ]]; then
    env_args+=(--setenv="XAUTHORITY=$XAUTHORITY")
  fi
  if [[ -n "${DBUS_SESSION_BUS_ADDRESS:-}" ]]; then
    env_args+=(--setenv="DBUS_SESSION_BUS_ADDRESS=$DBUS_SESSION_BUS_ADDRESS")
  fi

  systemctl --user reset-failed "$unit.service" >/dev/null 2>&1 || true
  systemd-run --user --unit="$unit" --collect "${env_args[@]}" "${cmd[@]}"
  echo "started mouse dictation daemon as $unit.service"
else
  nohup "${cmd[@]}" >"$log_file" 2>&1 &
  pid=$!
  echo "$pid" >"$pid_file"
  echo "started mouse dictation daemon pid=$pid"
fi
