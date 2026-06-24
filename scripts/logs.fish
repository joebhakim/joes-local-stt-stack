#!/usr/bin/env fish
set -l script_dir (dirname (status --current-filename))
source "$script_dir/env.fish"

set -l follow 0
set -l lines 120
for arg in $argv
    switch "$arg"
        case -f --follow
            set follow 1
        case -h --help
            echo "Usage: fish scripts/logs.fish [--follow] [lines]"
            exit 0
        case '*'
            if string match -qr '^[0-9]+$' -- "$arg"
                set lines "$arg"
            else
                echo "error: unknown argument: $arg" >&2
                echo "Usage: fish scripts/logs.fish [--follow] [lines]" >&2
                exit 2
            end
    end
end

if test $follow -eq 1
    echo "Following user journal for personal STT services. Press Ctrl+C to stop."
    journalctl --user \
        -u "$PERSONAL_STT_DAEMON_UNIT.service" \
        -u "$PERSONAL_STT_ANDROID_BRIDGE_UNIT.service" \
        -u "$PERSONAL_STT_MOUSE_UNIT.service" \
        -u "$PERSONAL_STT_KEYBOARD_UNIT.service" \
        -u "$PERSONAL_STT_TRAY_UNIT.service" \
        -f
    exit $status
end

if test -f "$PERSONAL_STT_ROOT/logs/dictation.log"
    echo "== logs/dictation.log =="
    tail -n "$lines" "$PERSONAL_STT_ROOT/logs/dictation.log"
else
    echo "logs/dictation.log not found"
end

if command -q journalctl
    echo
    echo "== user journal =="
    journalctl --user \
        -u "$PERSONAL_STT_DAEMON_UNIT.service" \
        -u "$PERSONAL_STT_ANDROID_BRIDGE_UNIT.service" \
        -u "$PERSONAL_STT_MOUSE_UNIT.service" \
        -u "$PERSONAL_STT_KEYBOARD_UNIT.service" \
        -u "$PERSONAL_STT_TRAY_UNIT.service" \
        -n "$lines" \
        --no-pager
end
