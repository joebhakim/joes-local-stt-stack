#!/usr/bin/env fish
set -l script_dir (dirname (status --current-filename))
source "$script_dir/env.fish"

set -l py "$PERSONAL_STT_PYTHON"
set -l cfg "$PERSONAL_STT_CONFIG"
set -l unit "$PERSONAL_STT_ANDROID_BRIDGE_UNIT"
set -l pid_file "$PERSONAL_STT_ROOT/state/android_bridge.pid"
set -l log_file "$PERSONAL_STT_ROOT/logs/android_bridge.out"
set -l port "$PERSONAL_STT_ANDROID_BRIDGE_PORT"

if command -q systemctl; and systemctl --user is-active --quiet "$unit.service"
    echo "android bridge already running as $unit.service"
    exit 0
end

if test -f "$pid_file"
    set -l old_pid (string trim -- (cat "$pid_file"))
    if test -n "$old_pid"
        kill -0 "$old_pid" >/dev/null 2>/dev/null
        if test $status -eq 0
            echo "android bridge already running (pid=$old_pid)"
            exit 0
        end
    end
    rm -f "$pid_file"
end

if not "$py" -c 'import websockets' >/dev/null 2>/dev/null
    echo "android bridge dependency missing: Python package 'websockets'"
    echo "Install dependencies before starting the bridge."
    exit 1
end

if command -q systemd-run
    systemctl --user reset-failed "$unit.service" >/dev/null 2>/dev/null
    systemd-run \
        --user \
        --unit=$unit \
        --collect \
        --working-directory="$PERSONAL_STT_ROOT" \
        "$py" "$PERSONAL_STT_ROOT/android_bridge.py" --config "$cfg" serve --port "$port" \
        >/dev/null
    echo "android bridge started as $unit.service on port $port"
    exit 0
end

nohup "$py" "$PERSONAL_STT_ROOT/android_bridge.py" --config "$cfg" serve --port "$port" >> "$log_file" 2>&1 &
set -l new_pid $last_pid
echo "$new_pid" > "$pid_file"
disown
echo "android bridge started (pid=$new_pid, port=$port)"
