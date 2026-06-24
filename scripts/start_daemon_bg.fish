#!/usr/bin/env fish
set -l script_dir (dirname (status --current-filename))
source "$script_dir/env.fish"

set -l py "$PERSONAL_STT_PYTHON"
set -l cfg "$PERSONAL_STT_CONFIG"
set -l unit "$PERSONAL_STT_DAEMON_UNIT"
set -l socket "$PERSONAL_STT_ROOT/state/dictation.sock"
set -l daemon_log "$PERSONAL_STT_ROOT/logs/daemon.out"
set -l dictation_log "$PERSONAL_STT_ROOT/logs/dictation.log"

if test -S "$socket"
    $py "$PERSONAL_STT_ROOT/dictatectl.py" --config "$cfg" status >/dev/null 2>/dev/null
    if test $status -eq 0
        echo "dictationd already running"
        exit 0
    end
    rm -f "$socket"
end

if command -q systemd-run
    systemctl --user reset-failed "$unit.service" >/dev/null 2>/dev/null
    set -l env_args "--setenv=LD_LIBRARY_PATH=$LD_LIBRARY_PATH"
    if set -q KITTY_LISTEN_ON
        set env_args $env_args "--setenv=KITTY_LISTEN_ON=$KITTY_LISTEN_ON"
    end
    if set -q KITTY_WINDOW_ID
        set env_args $env_args "--setenv=KITTY_WINDOW_ID=$KITTY_WINDOW_ID"
    end
    if set -q KITTY_PUBLIC_KEY
        set env_args $env_args "--setenv=KITTY_PUBLIC_KEY=$KITTY_PUBLIC_KEY"
    end
    systemd-run \
        --user \
        --unit=$unit \
        --collect \
        --working-directory="$PERSONAL_STT_ROOT" \
        $env_args \
        $py "$PERSONAL_STT_ROOT/dictate.py" daemon --config "$cfg" \
        >/dev/null
else
    nohup $py "$PERSONAL_STT_ROOT/dictate.py" daemon --config "$cfg" >> "$daemon_log" 2>&1 &
    disown
end

set -l started 0
for poll_idx in (seq 1 200)
    if test -S "$socket"
        $py "$PERSONAL_STT_ROOT/dictatectl.py" --config "$cfg" status >/dev/null 2>&1
        if test $status -eq 0
            set started 1
            break
        end
    end
    sleep 0.1
end

if test $started -eq 1
    $py "$PERSONAL_STT_ROOT/dictatectl.py" --config "$cfg" status
    exit 0
end

echo "dictationd did not become ready in time."
if test -f "$daemon_log"
    echo "--- $daemon_log (tail) ---"
    tail -n 80 "$daemon_log"
end
if test -f "$dictation_log"
    echo "--- $dictation_log (tail) ---"
    tail -n 80 "$dictation_log"
end
exit 1
