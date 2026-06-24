#!/usr/bin/env fish
set -l script_dir (dirname (status --current-filename))
source "$script_dir/env.fish"

set -l pid_file "$PERSONAL_STT_ROOT/state/android_bridge.pid"
set -l unit "$PERSONAL_STT_ANDROID_BRIDGE_UNIT"
set -l stopped 0

if command -q systemctl; and systemctl --user is-active --quiet "$unit.service"
    systemctl --user stop "$unit.service" >/dev/null 2>/dev/null
    systemctl --user reset-failed "$unit.service" >/dev/null 2>/dev/null
    set stopped 1
end

if test -f "$pid_file"
    set -l pid (string trim -- (cat "$pid_file"))
    if test -n "$pid"
        kill -0 "$pid" >/dev/null 2>/dev/null
        if test $status -eq 0
            kill "$pid" >/dev/null 2>/dev/null
            set stopped 1
        end
    end
    rm -f "$pid_file"
end

if test $stopped -eq 1
    echo "android bridge stopped"
else
    echo "android bridge already stopped"
end
