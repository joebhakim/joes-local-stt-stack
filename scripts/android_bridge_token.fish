#!/usr/bin/env fish
set -l script_dir (dirname (status --current-filename))
source "$script_dir/env.fish"

$PERSONAL_STT_PYTHON "$PERSONAL_STT_ROOT/android_bridge.py" --config "$PERSONAL_STT_CONFIG" token
