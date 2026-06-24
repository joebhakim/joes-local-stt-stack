#!/usr/bin/env fish
set -l script_dir (dirname (status --current-filename))
source "$script_dir/env.fish"

set -l pid_file "$PERSONAL_STT_ROOT/state/tray.pid"
set -l unit "$PERSONAL_STT_TRAY_UNIT"
set -l stopped 0

if command -q systemctl; and systemctl --user is-active --quiet "$unit.service"
    systemctl --user stop "$unit.service" >/dev/null 2>/dev/null
    set stopped 1
end

if test -f "$pid_file"
    set -l pid (string trim -- (cat "$pid_file"))
    if test -n "$pid"
        kill -0 "$pid" >/dev/null 2>/dev/null
        if test $status -eq 0
            kill "$pid" >/dev/null 2>/dev/null
            for poll_idx in (seq 1 20)
                sleep 0.05
                kill -0 "$pid" >/dev/null 2>/dev/null
                if test $status -ne 0
                    break
                end
            end
            kill -0 "$pid" >/dev/null 2>/dev/null
            if test $status -eq 0
                kill -9 "$pid" >/dev/null 2>/dev/null
            end
            set stopped 1
        end
    end
    rm -f "$pid_file"
end

if test $stopped -eq 0
    set -l pids (pgrep -f "$PERSONAL_STT_ROOT/tray.py")
    if test (count $pids) -gt 0
        kill $pids >/dev/null 2>/dev/null
        set stopped 1
    end
end

if test $stopped -eq 1
    echo "tray stopped"
else
    echo "tray already stopped"
end
