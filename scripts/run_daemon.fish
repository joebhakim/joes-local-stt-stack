#!/usr/bin/env fish
set -l script_dir (dirname (status --current-filename))
source "$script_dir/env.fish"

set -l socket "$PERSONAL_STT_ROOT/state/dictation.sock"
if test -S "$socket"
    $PERSONAL_STT_PYTHON "$PERSONAL_STT_ROOT/dictatectl.py" --config "$PERSONAL_STT_CONFIG" status >/dev/null 2>/dev/null
    if test $status -eq 0
        echo "dictationd already running"
        exit 0
    end
    rm -f "$socket"
end

$PERSONAL_STT_PYTHON "$PERSONAL_STT_ROOT/dictate.py" daemon --config "$PERSONAL_STT_CONFIG"
