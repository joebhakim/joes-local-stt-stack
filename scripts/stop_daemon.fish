#!/usr/bin/env fish
set -l script_dir (dirname (status --current-filename))
source "$script_dir/env.fish"

set -l unit "$PERSONAL_STT_DAEMON_UNIT"

$PERSONAL_STT_PYTHON "$PERSONAL_STT_ROOT/dictatectl.py" --config "$PERSONAL_STT_CONFIG" status >/dev/null 2>/dev/null
if test $status -ne 0
    systemctl --user stop "$unit.service" >/dev/null 2>/dev/null
    systemctl --user reset-failed "$unit.service" >/dev/null 2>/dev/null
    echo "dictationd already stopped"
    rm -f "$PERSONAL_STT_ROOT/state/dictation.sock" "$PERSONAL_STT_ROOT/state/dictation.pid"
    exit 0
end

$PERSONAL_STT_PYTHON "$PERSONAL_STT_ROOT/dictatectl.py" --config "$PERSONAL_STT_CONFIG" shutdown
systemctl --user stop "$unit.service" >/dev/null 2>/dev/null
systemctl --user reset-failed "$unit.service" >/dev/null 2>/dev/null
rm -f "$PERSONAL_STT_ROOT/state/dictation.sock" "$PERSONAL_STT_ROOT/state/dictation.pid"
