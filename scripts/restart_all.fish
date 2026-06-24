#!/usr/bin/env fish
set -l script_dir (dirname (status --current-filename))
source "$script_dir/env.fish"

set -l force 0
for arg in $argv
    switch "$arg"
        case -f --force
            set force 1
        case -h --help
            echo "Usage: fish scripts/restart_all.fish [--force]"
            echo
            echo "Without --force, refuses to restart while a recording is active."
            exit 0
        case '*'
            echo "error: unknown argument: $arg" >&2
            echo "Usage: fish scripts/restart_all.fish [--force]" >&2
            exit 2
    end
end

set -l status_json
set status_json ($PERSONAL_STT_PYTHON "$PERSONAL_STT_ROOT/dictatectl.py" --config "$PERSONAL_STT_CONFIG" --json status 2>/dev/null | string collect)
if test $status -eq 0; and string match -q '*"recording": true*' -- "$status_json"; and test $force -eq 0
    echo "error: daemon is recording; commit/cancel first, or rerun with --force to cancel and restart." >&2
    exit 2
end

if test $force -eq 1; and string match -q '*"recording": true*' -- "$status_json"
    echo "Cancelling active recording before restart..."
    $PERSONAL_STT_PYTHON "$PERSONAL_STT_ROOT/dictatectl.py" --config "$PERSONAL_STT_CONFIG" stop-cancel >/dev/null 2>/dev/null
end

fish "$script_dir/stop_all.fish"

echo
fish "$script_dir/start_all.fish"

