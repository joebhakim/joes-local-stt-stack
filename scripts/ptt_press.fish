#!/usr/bin/env fish
set -l script_dir (dirname (status --current-filename))
source "$script_dir/env.fish"

set -l py "$PERSONAL_STT_PYTHON"
set -l cfg "$PERSONAL_STT_CONFIG"

$py "$PERSONAL_STT_ROOT/dictatectl.py" --config "$cfg" status >/dev/null 2>/dev/null
if test $status -ne 0
    fish "$script_dir/start_daemon_bg.fish" >/dev/null
    $py "$PERSONAL_STT_ROOT/dictatectl.py" --config "$cfg" status >/dev/null 2>/dev/null
    if test $status -ne 0
        echo "error: daemon not reachable"
        exit 1
    end
end

set -l status_line ($py "$PERSONAL_STT_ROOT/dictatectl.py" --config "$cfg" status)
if string match -q "*recording=True*" -- "$status_line"
    echo "already recording"
    exit 0
end

$py "$PERSONAL_STT_ROOT/dictatectl.py" --config "$cfg" start $argv
