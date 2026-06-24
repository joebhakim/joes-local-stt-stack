#!/usr/bin/env fish
set -l script_dir (dirname (status --current-filename))
source "$script_dir/env.fish"

if test (count $argv) -gt 0
    $PERSONAL_STT_PYTHON "$PERSONAL_STT_ROOT/dictatectl.py" --config "$PERSONAL_STT_CONFIG" safe-test $argv
else
    $PERSONAL_STT_PYTHON "$PERSONAL_STT_ROOT/dictatectl.py" --config "$PERSONAL_STT_CONFIG" safe-test
end
