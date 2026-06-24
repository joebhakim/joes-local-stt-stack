#!/usr/bin/env bash
set -euo pipefail

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
pid_file="$root/state/x11_keyboard_dictation.pid"
log_file="$root/logs/x11_keyboard_dictation.daemon.out"
unit="${PERSONAL_STT_KEYBOARD_UNIT:-personal-stt-keyboard-dictation}"
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
  echo "keyboard dictation daemon already running as $unit.service"
  exit 0
fi

if [[ -f "$pid_file" ]]; then
  old_pid="$(cat "$pid_file")"
  if [[ -n "$old_pid" ]] && kill -0 "$old_pid" 2>/dev/null; then
    echo "keyboard dictation daemon already running pid=$old_pid"
    exit 0
  fi
  rm -f "$pid_file"
fi

cmd=(
  "$python_bin"
  "$root/scripts/x11_keyboard_dictation.py"
  --keys ""
  --modifier none
  --toggle-command "/usr/bin/fish $root/scripts/ptt_toggle_commit.fish"
  --binding none XF86HomePage home-start "/usr/bin/fish $root/scripts/ptt_press_profile.fish current"
  --binding none XF86Mail mail-commit "/usr/bin/fish $root/scripts/ptt_release_commit.fish"
  --binding none XF86PowerOff power-debug "/usr/bin/fish $root/scripts/ptt_press_debug.fish"
  --binding none XF86PowerDown power-debug "/usr/bin/fish $root/scripts/ptt_press_debug.fish"
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
  echo "started keyboard dictation daemon as $unit.service"
else
  nohup "${cmd[@]}" >"$log_file" 2>&1 &
  pid=$!
  echo "$pid" >"$pid_file"
  echo "started keyboard dictation daemon pid=$pid"
fi
