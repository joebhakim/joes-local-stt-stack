#!/usr/bin/env fish
set -l script_dir (dirname (status --current-filename))
source "$script_dir/env.fish"

set -l py "$PERSONAL_STT_PYTHON"
if not "$py" -c 'import PyQt6' >/dev/null 2>/dev/null; and not "$py" -c 'import PySide6' >/dev/null 2>/dev/null
    set -l system_py /usr/bin/python3
    if test -x "$system_py"; and begin; "$system_py" -c 'import PyQt6' >/dev/null 2>/dev/null; or "$system_py" -c 'import PySide6' >/dev/null 2>/dev/null; end
        set py "$system_py"
    end
end
set -l pid_file "$PERSONAL_STT_ROOT/state/tray.pid"
set -l log_file "$PERSONAL_STT_ROOT/logs/tray.out"
set -l unit "$PERSONAL_STT_TRAY_UNIT"

if command -q systemctl; and systemctl --user is-active --quiet "$unit.service"
    echo "tray already running as $unit.service"
    exit 0
end

if test -f "$pid_file"
    set -l old_pid (string trim -- (cat "$pid_file"))
    if test -n "$old_pid"
        kill -0 "$old_pid" >/dev/null 2>/dev/null
        if test $status -eq 0
            echo "tray already running (pid=$old_pid)"
            exit 0
        end
    end
    rm -f "$pid_file"
end

if command -q systemd-run
    set -l env_args "--setenv=DISPLAY=$DISPLAY"
    if set -q XAUTHORITY
        set env_args $env_args "--setenv=XAUTHORITY=$XAUTHORITY"
    end
    if set -q DBUS_SESSION_BUS_ADDRESS
        set env_args $env_args "--setenv=DBUS_SESSION_BUS_ADDRESS=$DBUS_SESSION_BUS_ADDRESS"
    end

    systemctl --user reset-failed "$unit.service" >/dev/null 2>/dev/null
    systemd-run \
        --user \
        --unit=$unit \
        --collect \
        --working-directory="$PERSONAL_STT_ROOT" \
        $env_args \
        "$py" "$PERSONAL_STT_ROOT/tray.py" --config "$PERSONAL_STT_CONFIG" \
        >/dev/null
    echo "tray started as $unit.service"
    exit 0
end

nohup "$py" "$PERSONAL_STT_ROOT/tray.py" --config "$PERSONAL_STT_CONFIG" >> "$log_file" 2>&1 &
set -l new_pid $last_pid
echo "$new_pid" > "$pid_file"
disown

sleep 0.4
kill -0 "$new_pid" >/dev/null 2>/dev/null
if test $status -eq 0
    echo "tray started (pid=$new_pid)"
    exit 0
end

echo "tray failed to start"
rm -f "$pid_file"
if test -f "$log_file"
    echo "--- $log_file (tail) ---"
    tail -n 80 "$log_file"
end
exit 1
